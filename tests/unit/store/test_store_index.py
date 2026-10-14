"""The store index (interfaces.md section 22): client allow-list, probes, classification, joins, caching, CLI.
Every test runs against the hermetic fake (fakeart.py): no network, no lab host."""
from __future__ import annotations

import datetime as dt
import hashlib
import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from fakeart import BUILD_REV, COMMIT, DOC_BYTES, RAW_A, RAW_B, ZST_A, FakeArtifactory, sample
from nanosoc_rm import contracts, db
from nanosoc_rm.config import ConfigError, load_config
from nanosoc_rm.store import client as C
from nanosoc_rm.store import index as I
from nanosoc_rm.store import views as V

URL = "http://fake.invalid/artifactory"
NOW = dt.datetime(2026, 10, 14, 9, 0, 0, tzinfo=dt.timezone.utc)


def st(**kw) -> C.Settings:
    base = dict(url=URL, release_repo="asic-release", token_file=None, token_expires=None, repos=(), projects={})
    base.update(kw)
    return C.Settings(**base)


def client_for(fa: FakeArtifactory, token: str = "t0k") -> C.StoreClient:
    return C.StoreClient(URL, token, transport=fa.transport())


def run(fa: FakeArtifactory, **kw) -> dict:
    with client_for(fa) as c:
        fact = I.build_index(c, st(**kw), now=NOW)
    assert contracts.validate("store", fact, "/$defs/store_index") == []
    return fact


def probe(fact: dict, feature: str) -> dict:
    return next(p for p in fact["probes"] if p["feature"] == feature)


# ---------------------------------------------------------------------------------------------------------------
# The allow-list (the tool's own: on OSS every token is effectively admin)
# ---------------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("method, path", [
    ("GET", "api/system/ping"), ("HEAD", "asic-release/x/y.json"), ("GET", "api/storage/asic-release"),
    ("POST", "api/search/aql"),
])
def test_the_allow_list_admits_reads_and_aql(method, path):
    C.check_request(method, path)


@pytest.mark.parametrize("method, path, why", [
    ("POST", "api/build", "allow-list"), ("PUT", "asic-release/a/b.json", "allow-list"),
    ("DELETE", "asic-release/a/b.json", "allow-list"), ("PATCH", "api/search/aql", "allow-list"),
    ("POST", "api/search/aql/../build", "leave"), ("GET", "access/api/v1/tokens", "Access"),
    ("GET", "api/security/token", "Access"), ("GET", "/artifactory/api/system/ping", "relative"),
    ("GET", "http://elsewhere.invalid/x", "relative"), ("GET", "a/../../access/api/v1/tokens", "leave"),
])
def test_the_allow_list_refuses_everything_else(method, path, why):
    with pytest.raises(C.StoreRefused) as e:
        C.check_request(method, path)
    assert why in str(e.value)


def test_a_put_is_admitted_only_for_the_publish_service_inside_the_release_repo():
    C.check_request("PUT", "asic-release/chipa/1.0.0/release-manifest.json", publish_repo="asic-release")
    for path in ("asic-candidate/x.json", "asic-release", "api/build", "asic-releasex/y.json"):
        with pytest.raises(C.StoreRefused):
            C.check_request("PUT", path, publish_repo="asic-release")
    with pytest.raises(C.StoreRefused):
        C.check_request("DELETE", "asic-release/a.json", publish_repo="asic-release")


def test_the_client_refuses_a_write_before_any_request(monkeypatch):
    fa = sample()
    with client_for(fa) as c:
        with pytest.raises(C.StoreRefused):
            c.get("access/api/v1/tokens")
    assert fa.log == []


def test_bearer_only_never_basic_never_netrc(tmp_path, monkeypatch):
    netrc = tmp_path / "netrc"
    netrc.write_text("machine fake.invalid login admin password secret\n")
    monkeypatch.setenv("NETRC", str(netrc))
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / ".netrc").write_text(netrc.read_text())
    fa = sample()
    run(fa)
    assert fa.auth_seen == {"Bearer"} and fa.basic_attempts == 0 and fa.writes == []
    assert {m for m, _ in fa.log} == {"GET", "POST"}
    assert [p for m, p in fa.log if m == "POST"] == ["/artifactory/api/search/aql"]


def test_a_redirect_is_not_followed_with_the_token():
    import httpx

    def handle(request):
        return httpx.Response(302, headers={"location": "http://other.invalid/steal"})
    with C.StoreClient(URL, "t0k", transport=httpx.MockTransport(handle)) as c:
        with pytest.raises(C.StoreError) as e:
            c.get("api/system/ping")
    assert e.value.status == 302 and "t0k" not in str(e.value)


def test_errors_never_carry_the_token():
    fa = sample(token="right")
    with client_for(fa, token="wrong-secret-token") as c:
        fact = I.build_index(c, st(), now=NOW)
    assert fact["verdict"] == "INFRA" and "refused the token" in fact["reason"]
    assert "wrong-secret-token" not in json.dumps(fact)


# ---------------------------------------------------------------------------------------------------------------
# One run against a full fake
# ---------------------------------------------------------------------------------------------------------------
def test_a_full_run_probes_every_feature_and_keeps_the_rows_a_page_needs():
    fact = run(sample())
    assert fact["verdict"] == "PASS" and fact["reason"] is None
    assert {p["feature"]: p["verdict"] for p in fact["probes"]} == {
        "system.ping": "PASS", "system.version": "PASS", "aql.items": "PASS", "aql.properties": "PASS",
        "build.list": "PASS", "build.info": "PASS", "storage.release_repo": "PASS", "download.manifest": "PASS"}
    roles = sorted((a["role"], a["name"]) for a in fact["artefacts"])
    assert roles == [("doc", "ERRATA.md"), ("doc", "secret_manual.pdf"), ("manifest", "tapeout-demo.json"),
                     ("signature", "tapeout-demo.json.sig"), ("source", "build_soc.tar.gz"),
                     ("stream", "a.gds.zst"), ("stream", "b.gds.zst")]
    assert fact["counts"]["files"] == 11 and fact["counts"]["with_commit"] == 2
    assert [(f["path"], f["files"]) for f in fact["foundry"]] == [("chipa/blk/_unbound/foundry/Archive_X", 2)]


def test_streams_join_by_raw_md5_never_by_the_stored_checksum():
    fact = run(sample())
    a = next(x for x in fact["artefacts"] if x["name"] == "a.gds.zst")
    assert a["raw_md5"] == RAW_A and a["md5"] == ZST_A and a["raw_md5_key"] == "pnr.raw_md5"
    b = next(x for x in fact["artefacts"] if x["name"] == "b.gds.zst")
    assert b["raw_md5"] == RAW_B and b["raw_md5_key"] == "submitted.raw_md5"
    ev = I.raw_md5_of("evidence", {"pnr.raw_md5": RAW_A})
    assert ev == (None, None) and I.subject_md5_of("evidence", {"pnr.raw_md5": RAW_A}) == RAW_A


def test_git_identity_is_what_the_store_records_and_nothing_else():
    fact = run(sample())
    a = next(x for x in fact["artefacts"] if x["name"] == "a.gds.zst")
    assert a["commit"] is None and a["git_repo"] is None                  # a path and a run tag are not a commit
    src = next(x for x in fact["artefacts"] if x["role"] == "source")
    assert (src["commit"], src["commit_key"]) == ("abcdef1", "chipa.commit")   # a short id stays short
    assert I.commit_of({"note": "abcdef1234"}) == (None, None)


def test_every_build_record_is_read_even_two_under_one_number():
    fact = run(sample())
    assert [(b["number"], b["vcs"][0]["revision"], b["dirty"], b["filed"]) for b in fact["builds"]] == [
        ("run1", BUILD_REV, True, 2), ("run1", COMMIT, False, 2)]
    assert fact["builds"][0]["vcs"][0]["host"] == "github" and fact["builds"][0]["vcs"][0]["path"] == "Demo-Org/Demo-Chip"


def test_the_manifest_is_read_and_its_signature_seen():
    m = run(sample())["manifests"][0]
    assert (m["kind"], m["configuration"], m["tag"], m["commit"], m["gds_raw_md5"], m["signature"]) == (
        "tapeout", "demo-si", "v1.0.0", COMMIT, RAW_A, "present")
    assert (m["foundry"], m["shuttle"], m["date"], m["foundry_verdict"]) == ("foundry-x", "mpw-1", "2026-09-01", "accepted")
    assert m["repo"] == {"host": "github", "path": "Demo-Org/Demo-Chip"} and m["problems"] == []


def test_a_release_manifest_reads_its_pins_and_lock_gds():
    body = json.dumps({"schema": 1, "kind": "release-manifest", "project": "chipa", "component": "demo-chip",
                       "configuration": "demo-si", "version": "1.0.0", "tag": "v1.0.0", "commit": COMMIT,
                       "repo": {"host": "gitlab", "path": "grp/demo"},
                       "pins": [{"tree_path": "ip/x", "component": "demo-ip", "repo": {"host": "github", "path": "O/x"},
                                 "commit": "cd" * 20}],
                       "outputs": {"gds": {"file": "chip.gds", "md5": RAW_A, "bytes": 3}},
                       "artefacts": [{"role": "stream", "path": "asic-candidate/a.gds.zst", "stored_md5": ZST_A,
                                      "raw_md5": RAW_A, "stored_sha256": "0" * 64, "bytes": 1, "raw_md5_from": "property"}]
                       }).encode()
    m = I.parse_manifest("chipa/1.0.0/release-manifest.json", "release-manifest.json", body)
    assert (m["kind"], m["repo"], m["gds_raw_md5"], m["pin_components"], m["problems"]) == (
        "release", {"host": "gitlab", "path": "grp/demo"}, RAW_A, {"demo-ip": "cd" * 20}, [])
    assert m["artefacts"] == [{"path": "asic-candidate/a.gds.zst", "role": "stream", "md5": RAW_A, "stored_md5": ZST_A}]


def test_a_manifest_missing_facts_names_each_gap():
    m = I.parse_manifest("p/tapeout-x.json", "tapeout-x.json", b'{"kind": "tapeout"}')
    assert set(m["problems"]) == {"no tag recorded", "no commit recorded", "no repo recorded",
                                  "no gds raw md5 recorded", "no foundry recorded", "no date recorded"}
    assert I.parse_manifest("p/x.json", "x.json", b"not json")["problems"] == ["not JSON: JSONDecodeError"]


# ---------------------------------------------------------------------------------------------------------------
# Features the server lacks: NOT-RUN with the reason, never a guess
# ---------------------------------------------------------------------------------------------------------------
def test_without_aql_the_file_index_is_not_run_and_the_index_unverified():
    fact = run(sample(aql=False))
    assert probe(fact, "aql.items")["verdict"] == "NOT-RUN" and "AQL is not available" in probe(fact, "aql.items")["detail"]
    assert fact["sections"]["items"] == {"verdict": "NOT-RUN", "reason": "AQL is not available on this server"}
    assert fact["sections"]["manifests"]["verdict"] == "NOT-RUN"
    assert fact["verdict"] == "UNVERIFIED" and fact["artefacts"] == []
    assert fact["builds"]                                                 # what the server has is still read


def test_without_build_info_the_builds_are_not_run():
    fact = run(sample(builds_api=False))
    assert probe(fact, "build.list")["verdict"] == "NOT-RUN" and fact["sections"]["builds"]["verdict"] == "NOT-RUN"
    assert fact["verdict"] == "PASS"                                      # the file index itself was complete


def test_a_missing_release_repository_is_named():
    fact = run(sample(repos={"asic-candidate", "asic-record"}))
    assert probe(fact, "storage.release_repo") == {"feature": "storage.release_repo", "verdict": "NOT-RUN",
                                                   "detail": "release repository asic-release does not exist"}
    assert fact["manifests"] == []


def test_no_build_records_at_all_is_an_answer_not_a_gap():
    fa = sample()
    fa.builds = {}
    fact = run(fa)
    assert probe(fact, "build.list")["verdict"] == "PASS" and fact["sections"]["builds"]["verdict"] == "PASS"


def test_an_unreachable_store_is_infra():
    fa = sample()
    fa.down = True
    fact = run(fa)
    assert fact["verdict"] == "INFRA" and probe(fact, "system.ping")["verdict"] == "FAIL"
    assert all(s["verdict"] == "NOT-RUN" for s in fact["sections"].values())


def test_a_cut_aql_answer_is_infra(monkeypatch):
    fa = sample()
    orig = fa._aql

    def cut(q):
        status, body, ctype = orig(q)
        doc = json.loads(body)
        doc["range"]["total"] = 9999
        return status, json.dumps(doc).encode(), ctype
    fa._aql = cut
    fact = run(fa)
    assert fact["sections"]["items"]["verdict"] == "INFRA" and fact["verdict"] == "INFRA"


def test_the_repos_setting_limits_the_aql_query():
    fa = sample()
    fact = run(fa, repos=("asic-candidate",))
    assert {a["repo"] for a in fact["artefacts"]} == {"asic-candidate"}


# ---------------------------------------------------------------------------------------------------------------
# Collateral cache
# ---------------------------------------------------------------------------------------------------------------
def test_documents_are_cached_verified_and_confidential_ones_withheld(tmp_path):
    fa = sample()
    with client_for(fa) as c:
        fact = I.build_index(c, st(), now=NOW)
        I.cache_collateral(c, fact, tmp_path)
    errata = next(a for a in fact["artefacts"] if a["name"] == "ERRATA.md")
    sha1 = hashlib.sha1(DOC_BYTES).hexdigest()
    assert errata["cached"] == sha1 and (tmp_path / sha1).read_bytes() == DOC_BYTES
    secret = next(a for a in fact["artefacts"] if a["name"] == "secret_manual.pdf")
    assert secret["cached"] is None and secret["cache_reason"].startswith("withheld")
    assert ("GET", "/artifactory/asic-record/chipa/blk/run1/docs/secret_manual.pdf") not in fa.log


def test_a_download_that_does_not_match_the_store_sha1_is_not_cached(tmp_path):
    fa = sample()
    fa.files["asic-record/chipa/blk/run1/docs/ERRATA.md"] = b"tampered"
    with client_for(fa) as c:
        fact = I.build_index(c, st(), now=NOW)
        I.cache_collateral(c, fact, tmp_path)
    errata = next(a for a in fact["artefacts"] if a["name"] == "ERRATA.md")
    assert errata["cached"] is None and "do not match" in errata["cache_reason"]
    assert not (tmp_path / hashlib.sha1(DOC_BYTES).hexdigest()).exists()
    assert not (tmp_path / hashlib.sha1(b"tampered").hexdigest()).exists()


# ---------------------------------------------------------------------------------------------------------------
# Config, store, CLI (a real local HTTP server: the whole path a deployment uses)
# ---------------------------------------------------------------------------------------------------------------
def write_cfg(tmp_path: Path, url: str, token: str = "t0k", extra: str = "") -> Path:
    tok = tmp_path / "store.token"
    tok.write_text(token + "\n")
    p = tmp_path / "rm.toml"
    p.write_text('[service]\ndata_dir = "%s"\n[stores.artifactory]\nurl = "%s"\ntoken_file = "%s"\n%s'
                 % (tmp_path / "data", url, tok, extra))
    return p


def test_config_keys_and_refusals(tmp_path):
    cfg = load_config(write_cfg(tmp_path, URL, extra='repos = ["asic-candidate"]\ntoken_expires = "2027-10-14"\n'
                                                       '[stores.artifactory.projects]\ndemo-chip = "chipa"\n'))
    s = C.settings(cfg)
    assert (s.release_repo, s.repos, s.token_expires, s.projects) == (
        "asic-release", ("asic-candidate",), "2027-10-14", {"demo-chip": "chipa"})
    for bad, want in (('url = "http://admin:pw@host/a"\n', "no user or token"),
                      ('token_expires = "soon"\n', "YYYY-MM-DD"),
                      ('[stores.artifactory.projects]\nx = "../y"\n', "store project name"),
                      ('url = "http://host/elsewhere"\n', "/artifactory")):
        p = tmp_path / "bad.toml"
        p.write_text('[service]\ndata_dir = "d"\n[stores.artifactory]\n%s' % bad)
        with pytest.raises(ConfigError) as e:
            load_config(p)
        assert want in str(e.value)


def test_an_empty_token_file_is_a_config_error_naming_no_content(tmp_path):
    cfg = load_config(write_cfg(tmp_path, URL, token=""))
    with pytest.raises(ConfigError) as e:
        C.StoreClient.from_config(cfg)
    assert "token file is empty" in str(e.value)


def test_the_cli_indexes_stores_and_answers_from_the_store(tmp_path, monkeypatch):
    from nanosoc_rm.cli import main
    fa = sample()
    with fa.serve() as url:
        cfg = write_cfg(tmp_path, url)
        monkeypatch.setenv("NANOSOC_RM_CONFIG", str(cfg))
        r = CliRunner().invoke(main, ["store", "index", "--store", "--json"], catch_exceptions=False)
    assert r.exit_code == 0, r.output
    doc = json.loads(r.output.strip().splitlines()[-1])
    assert doc["kind"] == "store_index" and doc["fact"]["verdict"] == "PASS"
    assert all(s["kind"] == "artifactory_api" and "t0k" not in s["command"] and "127.0.0.1" not in s["command"]
               for s in doc["sources"])
    assert "t0k" not in r.output and "127.0.0.1" not in json.dumps(doc)
    assert db.latest("store_index", config=load_config(cfg))[0]["fact"]["counts"]["files"] == 11
    assert fa.basic_attempts == 0 and fa.writes == []
    r2 = CliRunner().invoke(main, ["store", "search", "a.gds", "--json"])
    assert r2.exit_code == 0 and json.loads(r2.output.splitlines()[0])["label"] == "a.gds.zst"
    r3 = CliRunner().invoke(main, ["store", "findings", "--json"])
    assert r3.exit_code == 1                                             # chipb's shipped stream is unregistered
    assert {json.loads(x)["kind"] for x in r3.output.splitlines()} >= {"tapeout_unregistered"}


def test_the_cli_exits_2_when_the_store_does_not_answer(tmp_path, monkeypatch):
    from nanosoc_rm.cli import main
    cfg = write_cfg(tmp_path, "http://127.0.0.1:9/artifactory")
    monkeypatch.setenv("NANOSOC_RM_CONFIG", str(cfg))
    r = CliRunner().invoke(main, ["store", "index"])
    assert r.exit_code == 2 and "INFRA" in r.output


def test_the_cli_without_a_store_table_is_a_usage_error(tmp_path, monkeypatch, rm_config):
    from nanosoc_rm.cli import main
    r = CliRunner().invoke(main, ["store", "index"])
    assert r.exit_code == 3 and "[stores] artifactory" in r.output + (r.stderr if hasattr(r, "stderr") else "")


def test_the_job_kind_runs_the_cli_with_store():
    from nanosoc_rm import jobs
    k = jobs.registered("store-index")
    assert k is not None and k.args(k.params({})) == ["store", "index", "--store", "--json"]
    with pytest.raises(ValueError):
        k.params({"url": "x"})


def test_the_schedule_queues_one_job_only_when_a_store_is_configured(tmp_path, monkeypatch):
    from nanosoc_rm import jobs, schedule
    job = next(j for j in schedule.JOBS if j.name == "store_index")
    assert job.every == "nightly" and job.enabled and job.phase == "v1"
    plain = tmp_path / "plain.toml"
    plain.write_text('[service]\ndata_dir = "%s"\n' % (tmp_path / "d1"))
    job.run(load_config(plain))
    assert jobs.jobs(load_config(plain)) == []
    cfg = load_config(write_cfg(tmp_path, URL))
    job.run(cfg)
    job.run(cfg)                                                        # one queued already: no second
    assert [(j["kind"], j["by"]) for j in jobs.jobs(cfg)] == [("store-index", "schedule")]


# ---------------------------------------------------------------------------------------------------------------
# Views: the joins pages show (registry functions replaced by a fixed answer)
# ---------------------------------------------------------------------------------------------------------------
def silicon_record(cid="demo-si", commit=COMMIT):
    return {"fact": {"id": cid, "record": {"id": cid, "component": "demo-chip", "commit": commit,
                                           "ref": {"tag": "v1.0.0"}, "frozen": True,
                                           "silicon": {"chip": "demo", "revision": "r0p0"}}}}


@pytest.fixture
def stored(tmp_path, monkeypatch):
    def make(fa=None, projects=None, records=None):
        extra = ""
        if projects:
            extra = "[stores.artifactory.projects]\n" + "".join('%s = "%s"\n' % kv for kv in projects.items())
        cfg = load_config(write_cfg(tmp_path, URL, extra=extra))
        with client_for(fa or sample()) as c:
            I.index(cfg, store=True, client=c)
        monkeypatch.setattr(V, "_cfg_records", lambda config: records if records is not None else [silicon_record()])
        monkeypatch.setattr(V, "_component_repo", lambda config, comp: {"host": "github", "path": "Demo-Org/Demo-Chip"}
                            if comp else None)
        return cfg
    return make


def test_taped_out_matches_the_manifest_to_the_store_and_the_lock(stored, monkeypatch):
    cfg = stored()
    monkeypatch.setattr(V, "_lock_gds", lambda config, cid: [{"version": "1.0.0", "md5": RAW_A, "bytes": 1}])
    t = V.taped_out(cfg, "demo-si")
    assert len(t["rows"]) == 1
    r = t["rows"][0]
    assert r["store"][0] == "IN STORE" and r["lock"][0] == "MATCH"
    assert r["link"] is None                                            # no [hosts.github] web: no link, no guess
    monkeypatch.setattr(V, "_lock_gds", lambda config, cid: [{"version": "1.0.0", "md5": "e" * 32, "bytes": 1}])
    assert V.taped_out(cfg, "demo-si")["rows"][0]["lock"][0] == "MISMATCH"


def test_a_manifest_naming_a_stream_the_store_lacks_is_a_mismatch(stored):
    fa = sample()
    fa.items = [i for i in fa.items if i["name"] != "a.gds.zst"]
    cfg = stored(fa)
    assert V.taped_out(cfg, "demo-si")["rows"][0]["store"][0] == "NOT IN STORE"
    f = [x for x in V.findings(cfg) if x["kind"] == "store_mismatch"]
    assert len(f) == 1 and f[0]["evidence"][0].endswith("the store holds no file there")


def test_findings_name_unarchived_silicon_and_unregistered_shipped_streams(stored):
    cfg = stored(records=[silicon_record(), silicon_record("other-si", "f" * 40)])
    kinds = sorted((f["kind"], f["about"]) for f in V.findings(cfg))
    assert kinds == [("release_not_archived", "other-si"), ("tapeout_unregistered", "chipb")]
    unreg = next(f for f in V.findings(cfg) if f["kind"] == "tapeout_unregistered")
    assert "tag chipb-rc, commit 999999999999" in unreg["evidence"][0]


def test_a_store_with_no_finding_raises_no_card(stored):
    fa = sample()
    fa.items = [i for i in fa.items if i["name"] != "b.gds.zst"]
    cfg = stored(fa)
    assert V.findings(cfg) == []


def test_commit_links_need_a_configured_https_host(stored, tmp_path):
    cfg = stored()
    assert V.commit_link(cfg, {"host": "github", "path": "Demo-Org/Demo-Chip"}, COMMIT) is None
    p = tmp_path / "h.toml"
    p.write_text('[service]\ndata_dir = "%s"\n[hosts.github]\nweb = "https://github.example"\n'
                 '[hosts.gitlab]\nweb = "https://gitlab.example"\n' % (tmp_path / "data"))
    c2 = load_config(p)
    assert V.commit_link(c2, {"host": "github", "path": "O/R"}, COMMIT) == "https://github.example/O/R/tree/" + COMMIT
    assert V.commit_link(c2, {"host": "gitlab", "path": "g/r"}, COMMIT) == "https://gitlab.example/g/r/-/tree/" + COMMIT
    assert V.commit_link(c2, {"host": "github", "path": "O/R"}, "not-a-sha") is None


def test_identity_says_so_when_nothing_is_recorded(stored):
    cfg = stored(projects={"demo-chip": "chipa"})
    t = V.taped_out(cfg, "demo-si")
    a = next(s for s in t["streams"] if s["a"]["name"] == "a.gds.zst")
    assert a["identity"]["text"] == V.NO_IDENTITY and a["identity"]["link"] is None and a["registered"]


def test_an_infra_run_keeps_the_last_good_rows(stored, tmp_path):
    cfg = stored()
    fa = sample()
    fa.down = True
    with client_for(fa) as c:
        I.index(cfg, store=True, client=c)
    shown, new = V.shown(cfg)
    assert new["fact"]["verdict"] == "INFRA" and shown["fact"]["verdict"] == "PASS"


def test_search_by_path_commit_and_md5(stored):
    cfg = stored()
    assert [h["label"] for h in V.search(cfg, "run9")] == ["b.gds.zst"]
    assert [h["label"] for h in V.search(cfg, "99999999")] == ["b.gds.zst"]
    assert {h["label"] for h in V.search(cfg, RAW_A[:8])} == {"a.gds.zst", "tapeout-demo.json"}
    assert [h["kind"] for h in V.search(cfg, BUILD_REV[:10])] == ["build"]
    assert V.search(cfg, "ab") == []


def test_timeline_merges_manifests_and_uploads_newest_first(stored):
    cfg = stored(projects={"demo-chip": "chipa"})
    tl = V.timeline(cfg, "demo-si")
    whats = [e["what"] for e in tl["events"]]
    assert "tapeout" in whats and "upload" in whats
    assert [e["at"] for e in tl["events"]] == sorted((e["at"] for e in tl["events"]), reverse=True)


def test_collateral_links_only_to_verified_copies(stored):
    cfg = stored()
    rows = {r["a"]["name"]: r for r in V.collateral(cfg)["rows"]}
    assert rows["ERRATA.md"]["href"] == "/store/file/" + hashlib.sha1(DOC_BYTES).hexdigest()
    assert rows["secret_manual.pdf"]["href"] is None
    assert V.cached_file(cfg, hashlib.sha1(DOC_BYTES).hexdigest())[0].read_bytes() == DOC_BYTES
    assert V.cached_file(cfg, "1" * 40) is None and V.cached_file(cfg, "../etc") is None


def test_versions_rows_link_each_tag_and_name_the_chips_it_went_into(stored, monkeypatch):
    sha = "9588f14bb0643089dccfa0421230f4ca079c91bf"
    rec = silicon_record("demo-si", sha)
    rec["fact"]["record"]["component"] = "nanosoc_arch_tech"
    cfg = stored(records=[rec])
    line = next(x for x in (Path(__file__).resolve().parents[3] / "schemas" / "examples" /
                            "observation.versioning.example.jsonl").read_text().splitlines() if '"tag_check"' in x)
    db.put(json.loads(line), config=cfg)
    sv = V.component_versions(cfg, "nanosoc_arch_tech")
    assert [(r["tag"], r["commit"], r["chips"]) for r in sv["rows"]] == [("v0.1.0", sha, ["demo-si (demo r0p0)"])]
    assert V.component_versions(cfg, "other")["rows"] == []


@pytest.mark.parametrize("path, name, role", [
    ("p/b/r1/review/docs-pack", "DATASHEET_RC.md", "doc"), ("p/b/r1/docs", "chip-reference-manual.tar.zst", "doc"),
    ("p/2026/guide/docs", "Platform_Guide.pdf", "doc"), ("p/b/r1/evidence", "xor_no_reference.txt", "evidence"),
    ("p/b/r1/docs", "README.md", "other"), ("p/b/r1/stream", "x.gds.zst", "stream"),
    ("p/b/r1/source", "build_soc.tar.gz", "source"), ("p/b/_unbound/foundry/Archive_1", "r.rpt", "foundry-return"),
])
def test_roles_by_path_convention(path, name, role):
    row = {"repo": "asic-record", "path": path, "name": name}
    assert I.role_of(row, {}, "asic-release") == role
    assert I.role_of(row, {"rm.role": "stream-submitted"}, "asic-release") == "stream"     # a producer's role wins
