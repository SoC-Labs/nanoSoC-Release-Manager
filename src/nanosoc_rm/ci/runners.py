"""The hardened runner's settings, recorded and checked (interfaces.md section 17.3 `nanosoc-rm runner`; D10, H1).

    runner_settings(client=None) -> [runner_settings observation], one per id in ci.allowed_runner_ids

Each runner is read with `GET runners/:id` (the poller's read token) and compared with the expected hardened
values: `ci.runner_expected` (run_untagged, locked, access_level, is_shared) plus `ci.runner_tag`, which must be
in the runner's tag_list. A setting that differs is one `drift` entry and raises a `runner_drift` alert (the
dashboard's runner status turns red). A runner the token cannot read is `readable: false`, `unverified`. Each
observation is stored as its own snapshot (scope `runner_settings:<id>`).
"""
from __future__ import annotations

import datetime as dt

from nanosoc_rm import clock, db
from nanosoc_rm.ci.client import CIClient, CIError, make_client
from nanosoc_rm.config import Config, load_config

__all__ = ["runner_settings", "drift", "SETTING_KEYS"]

SETTING_KEYS = ("description", "active", "paused", "is_shared", "run_untagged", "locked", "access_level", "tag_list")


def drift(settings: dict, expected: dict) -> list[str]:
    """One entry per expected setting the runner does not have (`tag` = must be in tag_list)."""
    out = []
    for key, want in expected.items():
        if key == "tag":
            if want not in (settings.get("tag_list") or []):
                out.append("tag_list %s lacks %s" % (settings.get("tag_list"), want))
        elif settings.get(key) != want:
            out.append("%s: %s, expected %s" % (key, _show(settings.get(key)), _show(want)))
    return out


def _show(v: object) -> str:
    return "true" if v is True else "false" if v is False else str(v)


def _settings(raw: dict) -> dict:
    """The API record reduced to the schema's settings, with GitLab's newer field names mapped."""
    s = {k: raw.get(k) for k in SETTING_KEYS}
    if s["paused"] is None and "active" in raw:
        s["paused"] = not raw["active"]
    if s["active"] is None and "paused" in raw:
        s["active"] = not raw["paused"]
    s["tag_list"] = list(raw.get("tag_list") or [])
    return s


def runner_settings(client: CIClient | None = None, *, config: Config | None = None,
                    now: dt.datetime | None = None) -> list[dict]:
    config = config if config is not None else load_config()
    client = client if client is not None else make_client(config)
    stamp = clock.iso(now)
    expected = dict(config.require("ci", "runner_expected"))
    if config.get("ci", {}).get("runner_tag"):
        expected["tag"] = config["ci"]["runner_tag"]
    out = []
    for rid in config.require("ci", "allowed_runner_ids"):
        command = client.command("GET", "runner", rid)
        try:
            raw = client.runner(rid)
            settings = _settings(raw) if isinstance(raw, dict) else None
        except CIError:
            settings = None
        if settings is None or any(settings[k] is None for k in SETTING_KEYS if k != "description"):
            fact = {"runner_id": rid, "readable": False, "settings": None, "expected": expected, "drift": [],
                    "verdict": "unverified"}
        else:
            d = drift(settings, expected)
            fact = {"runner_id": rid, "readable": True, "settings": settings, "expected": expected, "drift": d,
                    "verdict": "drift" if d else "ok"}
        scope = "runner_settings:%s" % rid
        doc = {"schema": 1, "kind": "runner_settings", "subject": scope, "observed_at": stamp,
               "sources": [{"kind": "ci_api", "command": command}],
               "snapshot": {"id": db.new_snapshot_id(scope), "scope": scope}, "fact": fact}
        db.put_snapshot(scope, [doc], config=config)
        if fact["verdict"] == "drift":
            subject = "alert:runner_drift:%s" % scope
            if not any(a["subject"] == subject for a in db.alerts(config=config)):
                db.raise_alert("runner_drift", scope, "runner %s drifted from its hardened settings: %s"
                               % (rid, "; ".join(fact["drift"])), [{"kind": "ci_api", "command": command}],
                               at=stamp, config=config)
        out.append(doc)
    return out
