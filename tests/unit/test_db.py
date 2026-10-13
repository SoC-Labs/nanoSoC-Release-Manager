"""nanosoc_rm.db: interfaces.md section 12 and README section 4."""
from __future__ import annotations

import copy
import dataclasses
import datetime as dt
import sqlite3

import pytest

from nanosoc_rm import contracts, db

from conftest import EXAMPLES

NONCE = "5f0c3b1e9a7d4c2b8e6f1a0d3c5b7e9f"


def examples(name):
    return [d for _, d in contracts.docs_in(EXAMPLES / name)]


def silicon():
    return [d for d in examples("observation.example.jsonl") if d["kind"] == "silicon_protection"]


def resnap(docs, snap_id):
    out = copy.deepcopy(docs)
    for d in out:
        d["snapshot"]["id"] = snap_id
    return out


def rows(rm_config, sql):
    con = sqlite3.connect(str(rm_config.parent / "data" / "rm.sqlite"))
    try:
        return con.execute(sql).fetchall()
    finally:
        con.close()


def test_every_valid_example_observation_is_stored(rm_config):
    docs = examples("observation.example.jsonl") + examples("pin-state.example.jsonl") \
        + examples("observation.t0-result-waived.example.jsonl") + examples("observation.compat-result.example.jsonl") \
        + examples("observation.quality.example.jsonl")
    for d in docs:
        db.put(d)
    counts = {k: len(db.latest(k)) for k in db.SET_KINDS + db.EVENT_KINDS}
    assert counts == {"pin_state": 3, "host_compare": 3, "containment": 1, "silicon_protection": 13,
                      "mirror_status": 1, "repo_inventory": 1, "runner_settings": 1, "credential_status": 1,
                      "registry_record": 1, "rewrite_alert": 1, "t0_result": 2, "alert": 1, "branch_state": 0,
                      "compat_result": 1, "quality_density": 1, "quality_finding": 1, "readme_check": 1,
                      "doc_score": 1, "matrix_cell": 0, "matrix_quarantine": 0}
    assert db.latest("silicon_protection", "silicon_protection:eth-rc4:ahb_qspi:")[0]["fact"]["state"] == "PROTECTED_BY_TAG"  # CR-2
    assert rows(rm_config, "PRAGMA journal_mode")[0][0] == "wal"


@pytest.mark.parametrize("path", sorted((EXAMPLES / "invalid").glob("observation.*")), ids=lambda p: p.name)
def test_every_invalid_example_observation_is_refused(rm_config, path):
    for _, doc in contracts.docs_in(path):
        with pytest.raises(db.ObservationRefused):
            db.put(doc)
    with db.connect() as con:
        assert con.execute("SELECT count(*) FROM observations").fetchone()[0] == 0


def test_subject_must_be_the_derived_key(rm_config):
    d = copy.deepcopy(silicon()[0])
    d["subject"] = d["subject"].replace("tidechart", "tidelink")
    with pytest.raises(db.ObservationRefused, match="not the key README section 4 derives"):
        db.put(d)
    for d in examples("observation.example.jsonl") + examples("pin-state.example.jsonl"):
        assert db.subject_of(d) == d["subject"]


def test_envelope_never_newer_than_its_data(rm_config):
    pin = copy.deepcopy(examples("pin-state.example.jsonl")[0])
    oldest = min(h["observed_at"] for h in pin["fact"]["hosts"].values())
    pin["observed_at"] = "2026-09-30T23:59:59Z"                  # newer than every host state: flattering
    with pytest.raises(db.ObservationRefused, match="newer than the oldest observation inside it"):
        db.put(pin)
    pin["observed_at"] = oldest
    db.put(pin)


def test_snapshot_rules(rm_config):
    d = copy.deepcopy(silicon()[0])
    del d["snapshot"]
    with pytest.raises(db.ObservationRefused):
        db.put(d)
    alert = copy.deepcopy([x for x in examples("observation.example.jsonl") if x["kind"] == "alert"][0])
    alert["snapshot"] = {"id": "x", "scope": "alert:x"}
    with pytest.raises(db.ObservationRefused, match="event kind alert carries no snapshot"):
        db.put(alert)


def test_put_snapshot_replaces_the_current_set(rm_config):
    first = silicon()
    sid = db.put_snapshot("silicon_protection:eth-rc4", first)
    assert sid == first[0]["snapshot"]["id"] and len(db.latest("silicon_protection")) == 13
    # The next run no longer produces the ahb_qspi row: it disappears from the current view (M3).
    second = resnap([d for d in first if ":ahb_qspi:" not in d["subject"]], "silicon_protection:eth-rc4@run2")
    db.put_snapshot("silicon_protection:eth-rc4", second)
    now = db.latest("silicon_protection")
    assert len(now) == 12 and not [d for d in now if ":ahb_qspi:" in d["subject"]]
    assert len(db.history(first[-1]["subject"])) == 1                   # history is kept
    # An empty run is a complete, empty snapshot.
    db.put_snapshot("silicon_protection:eth-rc4", [])
    assert db.latest("silicon_protection") == []


def test_two_empty_runs_in_one_second_are_both_stored(rm_config, monkeypatch):
    """Integration defect (analysis CR): an empty run's id was `<scope>@<now to the second>`, so a second empty
    run of the same scope in the same second was refused as a duplicate id (the CLI showed an internal error)."""
    from nanosoc_rm import clock
    monkeypatch.setattr(clock, "iso", lambda *a, **k: "2026-10-02T12:00:00Z")    # one frozen second
    first = db.put_snapshot("silicon_protection:eth-rc4", [])
    second = db.put_snapshot("silicon_protection:eth-rc4", [])
    assert first != second
    assert first.startswith("silicon_protection:eth-rc4@2026-10-02T12:00:00Z#")
    assert rows(rm_config, "SELECT count(*) FROM snapshots WHERE scope = 'silicon_protection:eth-rc4' "
                           "AND completed_at IS NOT NULL")[0][0] == 2
    assert db.latest("silicon_protection") == []


def test_put_snapshot_is_atomic(rm_config):
    good = silicon()
    db.put_snapshot("silicon_protection:eth-rc4", good)
    broken = resnap(good, "silicon_protection:eth-rc4@broken")
    broken[5]["subject"] = "silicon_protection:eth-rc4:wrong"
    with pytest.raises(db.ObservationRefused, match="doc 5"):
        db.put_snapshot("silicon_protection:eth-rc4", broken)
    assert len(db.latest("silicon_protection")) == 13                   # the previous run stays current
    assert rows(rm_config, "SELECT count(*) FROM snapshots WHERE id LIKE '%broken'")[0][0] == 0
    with pytest.raises(db.ObservationRefused, match="already exists"):
        db.put_snapshot("silicon_protection:eth-rc4", good)
    with pytest.raises(db.ObservationRefused, match="snapshot scope"):
        db.put_snapshot("silicon_protection:other", resnap(good, "s3"))
    mixed = resnap(good, "s4")
    mixed[0]["snapshot"]["id"] = "s5"
    with pytest.raises(db.ObservationRefused, match="several ids"):
        db.put_snapshot("silicon_protection:eth-rc4", mixed)
    with pytest.raises(db.ObservationRefused, match="not <set kind>"):
        db.put_snapshot("t0_result:eth-rc4", [])


def test_incomplete_snapshot_is_never_current(rm_config):
    db.put_snapshot("silicon_protection:eth-rc4", silicon())
    con = sqlite3.connect(str(rm_config.parent / "data" / "rm.sqlite"))
    con.execute("INSERT INTO snapshots (id, scope, kind, completed_at) VALUES ('crashed', "
                "'silicon_protection:eth-rc4', 'silicon_protection', NULL)")
    con.execute("INSERT INTO observations (kind, subject, observed_at, snapshot_id, doc) VALUES "
                "('silicon_protection', 'silicon_protection:eth-rc4:x', '2026-10-01T00:00:00Z', 'crashed', '{}')")
    # A row inserted around put() with no snapshot at all (AT-A3's control does this) is never current either.
    con.execute("INSERT INTO observations (kind, subject, observed_at, doc) VALUES "
                "('silicon_protection', 'silicon_protection:eth-rc4:y', '2026-10-01T00:00:00Z', '{}')")
    # A scope whose only snapshot never completed has no current facts at all.
    con.execute("INSERT INTO snapshots (id, scope, kind, completed_at) VALUES ('crashed2', "
                "'silicon_protection:eth-main', 'silicon_protection', NULL)")
    con.execute("INSERT INTO observations (kind, subject, observed_at, snapshot_id, doc) VALUES "
                "('silicon_protection', 'silicon_protection:eth-main:z', '2026-10-01T00:00:00Z', 'crashed2', '{}')")
    con.commit()
    con.close()
    subjects = {d["subject"] for d in db.latest("silicon_protection")}
    assert len(subjects) == 13 and not {"silicon_protection:eth-rc4:x", "silicon_protection:eth-rc4:y",
                                        "silicon_protection:eth-main:z"} & subjects


def test_put_snapshot_failing_midway_stores_nothing(rm_config, monkeypatch):
    good = silicon()
    db.put_snapshot("silicon_protection:eth-rc4", good)
    real, calls = db._insert, []

    def fail_third(con, doc):
        calls.append(doc["subject"])
        if len(calls) == 3:
            raise OSError("disk full")
        real(con, doc)

    monkeypatch.setattr(db, "_insert", fail_third)
    with pytest.raises(OSError):
        db.put_snapshot("silicon_protection:eth-rc4", resnap(good[:5], "silicon_protection:eth-rc4@midway"))
    monkeypatch.setattr(db, "_insert", real)
    assert len(db.latest("silicon_protection")) == 13                   # the previous run stays current
    assert rows(rm_config, "SELECT count(*) FROM snapshots WHERE id LIKE '%midway'")[0][0] == 0
    assert rows(rm_config, "SELECT count(*) FROM observations")[0][0] == 13


def test_event_kinds_keep_the_latest_per_subject(rm_config):
    t0 = [d for d in examples("observation.example.jsonl") if d["kind"] == "t0_result"][0]
    db.put(t0)
    newer = copy.deepcopy(t0)
    newer["observed_at"] = "2026-10-01T03:00:00Z"
    newer["fact"]["reason"] = "newer"
    db.put(newer)
    older = copy.deepcopy(t0)
    older["observed_at"] = "2026-10-01T01:00:00Z"
    older["fact"]["reason"] = "older"
    db.put(older)
    assert [d["fact"]["reason"] for d in db.latest("t0_result")] == ["newer"]
    tie = copy.deepcopy(newer)
    tie["fact"]["reason"] = "tie, stored last"
    db.put(tie)
    assert [d["fact"]["reason"] for d in db.latest("t0_result", "t0_result:eth-rc4:")] == ["tie, stored last"]
    assert db.latest("t0_result", "t0_result:eth-main:") == []
    with pytest.raises(ValueError):
        db.latest("no_such_kind")


def test_data_age_is_from_the_oldest_fact():
    obs = silicon()
    now = dt.datetime(2026, 10, 1, 12, 0, 0, tzinfo=dt.timezone.utc)
    oldest = min(d["observed_at"] for d in obs)
    assert oldest == "2026-09-30T17:31:59Z"
    assert db.data_age(obs, now=now) == now - dt.datetime(2026, 9, 30, 17, 31, 59, tzinfo=dt.timezone.utc)
    with pytest.raises(ValueError):
        db.data_age([], now=now)


@dataclasses.dataclass
class RunRecord:                       # the shape of nanosoc_rm.ci.verify.RunRecord (interfaces.md section 10)
    nonce: str
    configuration: str
    commit: str
    request: bytes
    trigger: str
    triggered_by: str
    triggered_at: str
    pipeline_id: int | None
    job_id: int | None
    state: str


def run_record(**kw):
    base = dict(nonce=NONCE, configuration="eth-rc4", commit="ab73e40924317e45e7b3b941f52a7ed35f20832e",
                request=b'{"schema":1}', trigger="manual", triggered_by="alice",
                triggered_at="2026-10-01T02:03:10Z", pipeline_id=None, job_id=None, state="pending")
    base.update(kw)
    return RunRecord(**base)


def test_runs(rm_config):
    db.put_run(run_record())
    db.put_run(run_record(pipeline_id=1001, job_id=2002, state="accepted"))
    got = db.get_run(NONCE)
    assert got["request"] == b'{"schema":1}' and (got["pipeline_id"], got["state"]) == (1001, "accepted")
    assert [r["nonce"] for r in db.runs("eth-rc4")] == [NONCE] and db.runs("eth-main") == []
    assert [r["nonce"] for r in db.runs(state="accepted")] == [NONCE]
    with pytest.raises(ValueError, match="request cannot change"):
        db.put_run(run_record(request=b'{"schema":1,"x":2}'))
    with pytest.raises(ValueError):
        db.put_run(run_record(nonce="short"))
    with pytest.raises(ValueError):
        db.put_run(run_record(nonce="a" * 32, triggered_by=""))
    db.put_run(dataclasses.asdict(run_record(nonce="b" * 32, trigger="schedule", triggered_by="schedule")))
    # The contracted column names, as AT-A2 reads them.
    assert rows(rm_config, "SELECT nonce, configuration, trigger, triggered_by, triggered_at FROM runs ORDER BY nonce")[0][0] == NONCE


def test_actions(rm_config):
    db.log_action("alice", "run_t0", "eth-rc4", at="2026-10-01T02:03:11Z")
    assert db.actions() == [{"at": "2026-10-01T02:03:11Z", "login": "alice", "action": "run_t0",
                             "target": "eth-rc4", "result": "ok"}]
    with pytest.raises(ValueError):
        db.log_action("alice", "push_tag", "eth-rc4")
    with pytest.raises(ValueError):
        db.log_action("", "run_t0", "eth-rc4")


def test_alerts(rm_config):
    src = [{"kind": "derived", "command": "nanosoc-rm mirror sync"}]
    a = db.raise_alert("mirror_failed", "mirror_status:gitlab:soclabs/ip", "fetch failed", src,
                       at="2026-10-01T02:00:00Z")
    assert a["subject"] == "alert:mirror_failed:mirror_status:gitlab:soclabs/ip"
    assert [d["subject"] for d in db.alerts()] == [a["subject"]]
    db.clear_alert(a["subject"], "alice", at="2026-10-01T02:05:00Z")
    assert db.alerts() == []
    cleared = db.alerts(open_only=False)[0]
    assert (cleared["fact"]["cleared_at"], cleared["fact"]["cleared_by"]) == ("2026-10-01T02:05:00Z", "alice")
    assert contracts.validate_observation(cleared) == []
    assert db.latest("alert")[0]["fact"]["cleared_by"] == "alice"
    assert [(x["action"], x["target"]) for x in db.actions()] == [("clear_alert", a["subject"])]
    db.clear_alert(a["subject"], "bob")                                   # already cleared: nothing changes
    assert len(db.actions()) == 1
    db.raise_alert("mirror_failed", "mirror_status:gitlab:soclabs/ip", "fetch failed again", src,
                   at="2026-10-01T02:15:00Z")                             # raised again: open again
    assert [d["fact"]["message"] for d in db.alerts()] == ["fetch failed again"]
    with pytest.raises(KeyError):
        db.clear_alert("alert:rewrite:nothing", "alice")


def test_config_keyword_needs_no_environment(tmp_path, monkeypatch):
    monkeypatch.delenv("NANOSOC_RM_CONFIG", raising=False)
    cfg = tmp_path / "c.toml"
    cfg.write_text('[service]\ndata_dir = "d"\n')
    db.put(silicon()[0], config=cfg)
    assert len(db.latest("silicon_protection", config=cfg)) == 1
    assert (tmp_path / "d" / "rm.sqlite").is_file()
