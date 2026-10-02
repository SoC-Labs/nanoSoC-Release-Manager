"""A1 and H6 at system level, offline, with the acceptance files' own fixtures and checkers.

- A1: AT-A1's negative control plus its baseline (the full catalogue: nothing unclassified, counts equal the hosts'
  own totals, exit 0) and the GET-only check on both fake hosts.
- H6: AT-H6's negative control builds its service with `offline_service`, which ends by calling the web module's
  `create_app`, so on this branch it reports XFAIL (needs web). The same estate, registry and config are built here
  without the app, and the AT's steps run unchanged, plus the `age` table check and the mirror_failed alert.
"""
from __future__ import annotations

import pytest
from mw_fixtures import load_at

import datetime as dt
import shutil


@pytest.fixture(scope="session")
def at_pins():
    return load_at("test_pins")


@pytest.fixture(scope="session")
def at_web():
    return load_at("test_scope_web")


@pytest.fixture
def hermetic(at_pins, tmp_path):
    hosts = at_pins.LocalHosts(tmp_path / "hermetic")
    yield hosts
    hosts.api.close()


def _service_without_app(at_web, tmp, github, gitlab, monkeypatch):
    """offline_service(seed=False) minus create_app (the web module) and the CI fake."""
    estate = at_web.local_estate(tmp / "estate")
    registry = at_web.fixture_registry(tmp / "registry")
    gb = {h: "file://%s" % p for h, p in estate["hosts"].items()}
    cfg_path = at_web.write_config(tmp / "config.toml", data=tmp / "data", registry=registry, github=github,
                                   gitlab=gitlab, git_bases=gb)
    monkeypatch.setenv("NANOSOC_RM_CONFIG", str(cfg_path))
    at_web.github_listing(github, [("SoC-Labs/soc", "public")])
    at_web.gitlab_listing(gitlab, [("soclabs/ip", "public")])
    return {"cfg_path": cfg_path, "estate": estate, "registry": registry}


def test_a1_negative_control_system_level(at_web, tmp_path, monkeypatch):
    gh, gl = at_web.FakeHost(), at_web.FakeHost()
    try:
        cat, listing = at_web.fixture_catalogue_and_listing()
        cat["components"] = [c for c in cat["components"] if c["id"] != "secworks-sha256-project"]
        at_web.github_listing(gh, [(p, "public") for h, p, _, _ in listing if h == "github"])
        at_web.gitlab_listing(gl, [(p, "public") for h, p, _, _ in listing if h == "gitlab"])
        registry = at_web.write_registry(tmp_path / "registry", catalogue=cat,
                                         eligibility=at_web.example("eligibility.example.yaml")[0],
                                         defects=at_web.example("defect.SL-DEF-0001.example.yaml"),
                                         errata=at_web.example("erratum.ETH-R0P0-ERR-001.example.yaml"),
                                         imports=at_web.example("import-ledger.example.yaml")[0])
        cfg_path = at_web.write_config(tmp_path / "config.toml", data=tmp_path / "data", registry=registry,
                                       github=gh, gitlab=gl)
        monkeypatch.setenv("NANOSOC_RM_CONFIG", str(cfg_path))
        rc, flagged, p = at_web.cli(cfg_path, "inventory", "--unclassified", "--json")
        assert rc == 1, p.stderr
        assert [d["subject"] for d in flagged] == ["repo_inventory:gitlab:soclabs/secworks-sha256-project"]
        rc, docs, _ = at_web.cli(cfg_path, "inventory", "--json")
        assert at_web.check_a1(docs, dict(at_web.OBSERVED_TOTALS), cat) == \
            ["unclassified: gitlab:soclabs/secworks-sha256-project"]
        assert rc == 1
        # the tool asked the fake GitLab for page 2 (101 projects at per_page=100)
        pages = [e["query"].get("page", ["1"])[0] for e in gl.log if e["path"].endswith("/groups/soclabs/projects")]
        assert "2" in pages, pages
        # and the fake hosts saw only GETs (A2)
        assert gh.writes() == [] and gl.writes() == []
        # the baseline: with the full catalogue nothing is unclassified and the counts equal the hosts' totals
        full, _ = at_web.fixture_catalogue_and_listing()
        registry2 = at_web.write_registry(tmp_path / "registry2", catalogue=full,
                                          eligibility=at_web.example("eligibility.example.yaml")[0])
        cfg2 = at_web.write_config(tmp_path / "config2.toml", data=tmp_path / "data2", registry=registry2,
                                   github=gh, gitlab=gl)
        rc, docs, _ = at_web.cli(cfg2, "inventory", "--json")
        assert rc == 0 and at_web.check_a1(docs, dict(at_web.OBSERVED_TOTALS), full) == []
        rc, flagged, _ = at_web.cli(cfg2, "inventory", "--unclassified", "--json")
        assert (rc, flagged) == (0, [])
    finally:
        gh.close()
        gl.close()


def test_h6_negative_control_system_level(at_web, tmp_path, monkeypatch):
    gh, gl = at_web.FakeHost(), at_web.FakeHost()
    try:
        svc = _service_without_app(at_web, tmp_path / "svc", gh, gl, monkeypatch)
        at_web.cli(svc["cfg_path"], "mirror", "sync")
        _, before, _ = at_web.cli(svc["cfg_path"], "mirror", "status", "--json")
        ip = next(d for d in before if d["fact"]["repo"]["path"] == "soclabs/ip")
        assert ip["fact"]["ok"], "baseline: the first sync succeeds"
        mirrors = at_web.data_dir(at_web.read_config(svc["cfg_path"])) / "mirrors"
        assert at_web.check_h6_refs(mirrors / "estate-ip.git") == []
        assert at_web.check_h6_refs(mirrors / "estate-soc.git") == []        # the PR ref is not mirrored
        shutil.move(str(svc["estate"]["ip"]), str(tmp_path / "ip-gone.git"))
        at_web.cli(svc["cfg_path"], "mirror", "sync")
        _, after, _ = at_web.cli(svc["cfg_path"], "mirror", "status", "--json")
        ip2 = next(d for d in after if d["fact"]["repo"]["path"] == "soclabs/ip")
        assert ip2["fact"]["ok"] is False and ip2["fact"]["last_success_at"] == ip["fact"]["last_success_at"]
        later = at_web.utc(ip["fact"]["last_success_at"]) + dt.timedelta(minutes=16)
        probs = at_web.check_h6_status([ip2], expected={("gitlab", "soclabs/ip")}, now=later, max_age_s=15 * 60)
        assert any("mirror age" in p for p in probs) and any("the last fetch failed" in p for p in probs), probs
        # the table form prints an age for every repo that was ever fetched (H6 done-when)
        _, _, table = at_web.cli(svc["cfg_path"], "mirror", "status")
        ok_docs = [d for d in after if d["fact"]["last_success_at"]]
        assert ok_docs and at_web.check_h6_table(table.stdout, ok_docs, now=at_web.now_utc()) == []
        # and a mirror_failed alert was raised once
        _, alerts, _ = at_web.cli(svc["cfg_path"], "alerts", "--json")
        assert "alert:mirror_failed:mirror_status:gitlab:soclabs/ip" in [a["subject"] for a in alerts]
    finally:
        gh.close()
        gl.close()
