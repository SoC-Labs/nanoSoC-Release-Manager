"""The store index in the GUI (interfaces.md section 22): /store pages, the Taped out panel, the History
timeline, Releases columns, inbox cards, search. Every GET reads stored facts: the tests prove no GET builds a
store client. Hermetic: the index is filled from the in-process fake (tests/unit/store/fakeart.py)."""
from __future__ import annotations

import hashlib
import re
import shutil
import sys
from pathlib import Path

import pytest
import yaml
from starlette.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "store"))
from fakeart import DOC_BYTES, make_signer, sample  # noqa: E402

from nanosoc_rm.config import load_config  # noqa: E402
from nanosoc_rm.store import client as C  # noqa: E402
from nanosoc_rm.store import index as I  # noqa: E402
from nanosoc_rm.web.app import create_app  # noqa: E402
from test_web_local import local_config, page_csrf, sign_in  # noqa: E402
from webfix import make_registry  # noqa: E402

TOKEN = "s3cret-store-token"
REPO = Path(__file__).resolve().parents[3]


@pytest.fixture
def store_app(tmp_path, monkeypatch, seeded_db):
    reg = make_registry(tmp_path / "registry")
    (tmp_path / "data").mkdir()
    shutil.copy(seeded_db, tmp_path / "data" / "rm.sqlite")
    (tmp_path / "store.token").write_text(TOKEN + "\n")
    keys = make_signer(tmp_path)
    extra = ('[stores.artifactory]\nurl = "http://127.0.0.1:9/artifactory"\ntoken_file = "%s"\nallowed_signers = "%s"\n'
             '[stores.artifactory.projects]\nnanosoc-ethernet-chiplet = "chipa"\n'
             % (tmp_path / "store.token", keys["allowed"]))
    cfg = local_config(tmp_path / "rm.toml", tmp_path / "data", reg, extra=extra)
    monkeypatch.setenv("NANOSOC_RM_CONFIG", str(cfg))
    fa = sample(configuration="eth-rc4", token=TOKEN, key=keys["key"])
    with C.StoreClient("http://fake.invalid/artifactory", TOKEN, transport=fa.transport()) as c:
        I.index(load_config(cfg), store=True, client=c)
    app = create_app(cfg)
    client = TestClient(app)
    assert sign_in(client).status_code == 303

    # From here on, no request may build a store client: a GET shows stored facts only.
    def refuse(*a, **kw):
        raise AssertionError("a request built a store client")
    monkeypatch.setattr(C.StoreClient, "__init__", refuse)
    return {"client": client, "cfg": cfg, "fa": fa, "data": tmp_path / "data"}


def test_the_store_page_shows_the_stored_index_and_no_secret(store_app):
    c = store_app["client"]
    r = c.get("/store/")
    assert r.status_code == 200
    t = r.text
    for want in ("a.gds.zst", "b.gds.zst", "tapeout-demo.json", "no git identity recorded", 'data-probe="aql.items"',
                 "Refresh the index", "ERRATA.md", "data-age-seconds", 'data-subject="store_index:artifactory"'):
        assert want in t, want
    assert TOKEN not in t and "127.0.0.1:9" not in t and "Bearer" not in t
    q = c.get("/store/?q=run9").text
    assert 'data-hit="stream"' in q and "b.gds.zst" in q


def test_a_document_downloads_from_the_verified_copy_only(store_app):
    c = store_app["client"]
    sha1 = hashlib.sha1(DOC_BYTES).hexdigest()
    r = c.get("/store/file/%s" % sha1)
    assert r.status_code == 200 and r.content == DOC_BYTES
    assert r.headers["content-type"] == "application/octet-stream" and "ERRATA.md" in r.headers["content-disposition"]
    assert c.get("/store/file/%s" % ("1" * 40)).status_code == 404
    assert c.get("/store/file/..%2F..%2Frm.sqlite").status_code == 404
    page = c.get("/store/").text
    assert "/store/file/%s" % sha1 in page and "withheld: confidentiality" in page


def test_refresh_queues_one_job_and_a_second_press_reuses_it(store_app):
    c = store_app["client"]
    tok = page_csrf(c, "/store/")
    r = c.post("/store/index", data={"csrf_token": tok}, follow_redirects=False)
    assert r.status_code == 303 and re.search(r"/store/\?job=\d+$", r.headers["location"])
    r2 = c.post("/store/index", data={"csrf_token": tok}, follow_redirects=False)
    assert r2.headers["location"] == r.headers["location"]
    assert c.post("/store/index", data={}, follow_redirects=False).status_code in (400, 403)
    from nanosoc_rm import jobs
    js = jobs.jobs(load_config(store_app["cfg"]))
    assert [(j["kind"], j["command"]) for j in js] == [("store-index", "nanosoc-rm store index --store")]


def test_taped_out_panel_and_timeline(store_app):
    c = store_app["client"]
    s = c.get("/configurations/eth-rc4/silicon").text
    assert "data-taped-out" in s and "tapeout-demo.json" in s and "IN STORE" in s and "foundry-x" in s
    assert "NO LOCK" in s                                                # no release draft of eth-rc4 here
    assert 'data-signature="verified"' in s and "VERIFIED" in s
    h = c.get("/configurations/eth-rc4/history").text
    assert "data-timeline" in h and 'data-event="tapeout"' in h and 'data-event="upload"' in h


def test_inbox_search_and_releases_show_store_facts(store_app):
    c = store_app["client"]
    inbox = c.get("/").text
    assert "Store project chipb holds 1 shipped stream" in inbox
    assert "eth-rc4" in inbox
    hits = c.get("/search?q=b.gds").text
    assert "store ·" in hits and "b.gds.zst" in hits
    rel = c.get("/releases/").text
    assert "Documents and collateral in the store" in rel and "ERRATA.md" in rel


def test_the_store_post_exists_only_in_a_local_deployment(store_app, tmp_path, fake_github):
    from nanosoc_rm.web import routers
    assert ("POST", "/store/index") in routers.writes(store_app["client"].app)
    from webfix import write_config
    from nanosoc_rm import auth
    cfg = write_config(tmp_path / "gh.toml", data=store_app["data"], github_url=fake_github.url)
    app = create_app(cfg, auth=auth.FakeMembership(login="alice"))
    assert not {w for w in routers.writes(app) if w[1].startswith("/store")}


def test_an_unsigned_record_shows_unverified_never_in_store(tmp_path, monkeypatch, seeded_db):
    """SI-1 in the GUI: a record whose .sig is junk shows its signature INVALID and its store state UNVERIFIED."""
    reg = make_registry(tmp_path / "registry")
    (tmp_path / "data").mkdir()
    shutil.copy(seeded_db, tmp_path / "data" / "rm.sqlite")
    (tmp_path / "store.token").write_text(TOKEN + "\n")
    keys = make_signer(tmp_path)
    extra = ('[stores.artifactory]\nurl = "http://127.0.0.1:9/artifactory"\ntoken_file = "%s"\nallowed_signers = "%s"\n'
             % (tmp_path / "store.token", keys["allowed"]))
    cfg = local_config(tmp_path / "rm.toml", tmp_path / "data", reg, extra=extra)
    monkeypatch.setenv("NANOSOC_RM_CONFIG", str(cfg))
    fa = sample(configuration="eth-rc4", token=TOKEN)                   # no key: the .sig beside the record is junk
    with C.StoreClient("http://fake.invalid/artifactory", TOKEN, transport=fa.transport()) as c:
        I.index(load_config(cfg), store=True, client=c)
    client = TestClient(create_app(cfg))
    assert sign_in(client).status_code == 303
    s = client.get("/configurations/eth-rc4/silicon").text
    assert 'data-signature="invalid"' in s and "UNVERIFIED" in s and ">IN STORE<" not in s
    assert "has no verified manifest" in client.get("/").text


def test_taped_out_panel_lists_the_registry_tapeout_drafts_of_this_configuration(store_app, tmp_path):
    """CR-WE-int: an unpublished tapeout draft in the registry's tapeouts/ shows on its configuration's Taped out
    panel with a link to its Publish page; a draft of another configuration does not; a broken draft says why."""
    from nanosoc_rm import contracts
    reg = tmp_path / "registry"
    (reg / "tapeouts").mkdir()
    doc = contracts.load_yaml((REPO / "schemas" / "examples" / "tapeout-record.draft.example.yaml").read_text())
    doc["record"]["configuration"] = "eth-rc4"
    (reg / "tapeouts" / "demo-r0.tapeout.yaml").write_text(yaml.safe_dump(doc))
    other = dict(doc, record=dict(doc["record"], configuration="eth-main"))
    (reg / "tapeouts" / "other-r0.tapeout.yaml").write_text(yaml.safe_dump(other))
    bad = dict(doc, record=dict(doc["record"], status="maybe"))
    (reg / "tapeouts" / "bad-r0.tapeout.yaml").write_text(yaml.safe_dump(bad))
    s = store_app["client"].get("/configurations/eth-rc4/silicon").text
    assert "data-tapeout-drafts" in s and 'href="/publish/tapeout/demo-r0"' in s
    assert "accepted-by-foundry" in s and doc["record"]["gds"]["raw_md5"] in s
    assert "other-r0" not in s
    assert 'data-tapeout-draft="bad-r0"' in s and "does not meet its contract" in s


def test_a_tapeout_draft_shows_without_a_store(tmp_path, monkeypatch, seeded_db):
    """CR-WE-int: with no [stores.artifactory] and no index, the Silicon tab still lists the registry's draft."""
    from nanosoc_rm import contracts
    reg = make_registry(tmp_path / "registry")
    (reg / "tapeouts").mkdir()
    doc = contracts.load_yaml((REPO / "schemas" / "examples" / "tapeout-record.draft.example.yaml").read_text())
    doc["record"]["configuration"] = "eth-rc4"
    (reg / "tapeouts" / "demo-r0.tapeout.yaml").write_text(yaml.safe_dump(doc))
    (tmp_path / "data").mkdir()
    shutil.copy(seeded_db, tmp_path / "data" / "rm.sqlite")
    cfg = local_config(tmp_path / "rm.toml", tmp_path / "data", reg)
    monkeypatch.setenv("NANOSOC_RM_CONFIG", str(cfg))
    client = TestClient(create_app(cfg))
    assert sign_in(client).status_code == 303
    s = client.get("/configurations/eth-rc4/silicon").text
    assert "data-taped-out" in s and 'href="/publish/tapeout/demo-r0"' in s
    assert "No store index is stored" in s
