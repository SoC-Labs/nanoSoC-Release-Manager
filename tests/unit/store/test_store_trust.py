"""The store index's trust and verdict branches (review of the store index: SI-1 to SI-4).

- A manifest in the release repository is trusted only when its SSHSIG signature verifies against the allowed
  signers and no shared login uploaded it. Anything else is never shown green and never silences a finding.
- Every decisive view and verdict branch has a test that fails when the branch is changed.
- The allow-list refuses encoded and Tomcat-style dot segments.

Hermetic: the fake Artifactory (fakeart.py); the signing key is made per run with ssh-keygen and never committed."""
from __future__ import annotations

import hashlib
import json

import pytest

from fakeart import RAW_A, ZST_A, item, make_signer, sample
from nanosoc_rm.config import load_config
from nanosoc_rm.store import client as C
from nanosoc_rm.store import index as I
from nanosoc_rm.store import views as V
from test_store_index import URL, client_for, probe, run, silicon_record, write_cfg

MAN = "asic-release/chipa/1.0.0/tapeout-demo.json"


@pytest.fixture(scope="module")
def signer(tmp_path_factory):
    return make_signer(tmp_path_factory.mktemp("signer"))


def trusted_kw(signer) -> dict:
    return {"allowed_signers": signer["allowed"], "shared_logins": ("soclabs",)}


@pytest.fixture
def stored(tmp_path, monkeypatch, signer):
    """Index `fa` into a config with the allowed signers (unless `signed=False`), and fix the registry answers."""
    def make(fa=None, records=None, allowed=True, shared=("soclabs",), lock=None):
        extra = ""
        if allowed:
            extra += 'allowed_signers = "%s"\n' % signer["allowed"]
        if shared:
            extra += "shared_logins = [%s]\n" % ", ".join('"%s"' % s for s in shared)
        cfg = load_config(write_cfg(tmp_path, URL, extra=extra))
        with client_for(fa or sample(key=signer["key"])) as c:
            I.index(cfg, store=True, client=c)
        monkeypatch.setattr(V, "_cfg_records", lambda config: records if records is not None else [silicon_record()])
        monkeypatch.setattr(V, "_component_repo", lambda config, comp: {"host": "github", "path": "Demo-Org/Demo-Chip"}
                            if comp else None)
        monkeypatch.setattr(V, "_lock_gds", lambda config, cid: lock if lock is not None else [])
        return cfg
    return make


def manifest_body(**over) -> bytes:
    doc = {"schema": 1, "kind": "tapeout-record", "tapeout_id": "demo-tapeout-1", "project": "chipa", "version": "1.0.0",
           "configuration": "demo-si", "foundry": {"foundry": "foundry-x"}, "submission_date": "2026-09-01",
           "source": {"repo": {"host": "github", "path": "Demo-Org/Demo-Chip"}, "tags": ["v1.0.0"], "commit": "1" * 40},
           "gds": {"raw_md5": RAW_A, "store_path": "asic-candidate/chipa/blk/run1/stream/a.gds.zst"}}
    for k, v in over.items():
        if v is None:
            doc.pop(k, None)
        else:
            doc[k] = v
    return json.dumps(doc, sort_keys=True).encode()


def with_manifest(fa, body: bytes, signer) -> None:
    """Replace the sample's tapeout record by `body`, signed by the allowed key."""
    from nanosoc_rm.publish import signing
    fa.files[MAN] = body
    fa.files[MAN + ".sig"] = signing.sign(body, signer["key"], ssh_keygen="/nonexistent").text.encode()
    for it in fa.items:
        if it["name"] == "tapeout-demo.json":
            it["size"], it["actual_sha1"] = len(body), hashlib.sha1(body).hexdigest()


# ---------------------------------------------------------------------------------------------------------------
# SI-1: a manifest is trusted only when its signature verifies and no shared login uploaded it
# ---------------------------------------------------------------------------------------------------------------
def test_a_signature_by_the_allowed_key_verifies(signer):
    m = run(sample(key=signer["key"]), **trusted_kw(signer))["manifests"][0]
    assert (m["signature"], m["trusted"], m["problems"]) == ("verified", True, [])
    assert m["signer"].startswith("SHA256:")


def test_junk_beside_a_manifest_is_an_invalid_signature_not_a_present_one(signer):
    m = run(sample(), **trusted_kw(signer))["manifests"][0]
    assert m["signature"] == "invalid" and m["trusted"] is False
    assert any(p.startswith("signature invalid") for p in m["problems"])


def test_a_signature_by_another_key_is_invalid(signer):
    m = run(sample(key=signer["other"]), **trusted_kw(signer))["manifests"][0]
    assert m["signature"] == "invalid" and "not an allowed signer" in m["signature_detail"]


def test_a_manifest_changed_after_signing_is_invalid(signer):
    fa = sample(key=signer["key"])
    fa.files[MAN] = fa.files[MAN].replace(b"foundry-x", b"foundry-y")
    assert run(fa, **trusted_kw(signer))["manifests"][0]["signature"] == "invalid"


def test_without_allowed_signers_a_signature_is_unverified(signer):
    m = run(sample(key=signer["key"]))["manifests"][0]
    assert (m["signature"], m["trusted"]) == ("unverified", False)
    assert "allowed_signers" in m["signature_detail"]


def test_a_manifest_without_a_signature_is_absent(signer):
    fa = sample(key=signer["key"])
    fa.items = [i for i in fa.items if not i["name"].endswith(".sig")]
    m = run(fa, **trusted_kw(signer))["manifests"][0]
    assert (m["signature"], m["trusted"]) == ("absent", False) and "signature absent" in m["problems"]


def test_a_manifest_uploaded_by_a_shared_login_is_not_trusted_even_when_signed(signer):
    m = run(sample(key=signer["key"], created_by="soclabs"), **trusted_kw(signer))["manifests"][0]
    assert m["signature"] == "verified" and m["trusted"] is False
    assert "uploaded by the shared login soclabs" in m["problems"]


def test_a_planted_record_is_never_green_and_never_silences_the_card(stored):
    """The review's reproduction: a shared login plants a tapeout record with a junk .sig."""
    cfg = stored(sample(created_by="soclabs"), lock=[{"version": "1.0.0", "md5": RAW_A, "bytes": 1}])
    r = V.taped_out(cfg, "demo-si")["rows"][0]
    assert r["store"][1] != "c-pass" and r["lock"][1] != "c-pass"
    assert r["store"][0] == "UNVERIFIED" and r["lock"][0] == "UNVERIFIED"
    kinds = {(f["kind"], f["about"]) for f in V.findings(cfg)}
    assert ("release_not_archived", "demo-si") in kinds
    mism = next(f for f in V.findings(cfg) if f["kind"] == "store_mismatch")
    assert any("signature" in e for e in mism["evidence"]) and any("shared login" in e for e in mism["evidence"])
    assert V.release_rows(cfg)[("demo-si", "1.0.0")]["store"][1] != "c-pass"


def test_an_untrusted_record_does_not_register_a_shipped_stream(stored):
    fa = sample()                                                        # junk signature
    fa.items[1]["properties"] = [{"key": "submitted.raw_md5", "value": RAW_A},     # chipb's stream ships RAW_A ...
                                 {"key": "submitted.ships", "value": "yes"}]
    cfg = stored(fa)
    assert "tapeout_unregistered" in {f["kind"] for f in V.findings(cfg)}         # ... and the record does not cover it


def test_a_trusted_record_is_green(stored):
    cfg = stored(lock=[{"version": "1.0.0", "md5": RAW_A, "bytes": 1}])
    r = V.taped_out(cfg, "demo-si")["rows"][0]
    assert (r["store"][:2], r["lock"][:2]) == (("IN STORE", "c-pass"), ("MATCH", "c-pass"))
    assert V.release_rows(cfg)[("demo-si", "1.0.0")]["store"][:2] == ("MATCH", "c-pass")
    assert "release_not_archived" not in {f["kind"] for f in V.findings(cfg)}


# ---------------------------------------------------------------------------------------------------------------
# SI-2: the decisive branches, one test each
# ---------------------------------------------------------------------------------------------------------------
def test_a_build_record_that_cannot_be_read_is_infra(signer):
    fa = sample(key=signer["key"])
    fa.fail["api/build/chipa-blk"] = 500
    fact = run(fa, **trusted_kw(signer))
    assert fact["sections"]["builds"]["verdict"] == "INFRA" and probe(fact, "build.info")["verdict"] == "FAIL"
    assert fact["verdict"] == "INFRA"


def test_a_manifest_that_cannot_be_downloaded_is_infra(signer):
    fa = sample(key=signer["key"])
    fa.fail[MAN] = 500
    fact = run(fa, **trusted_kw(signer))
    assert fact["sections"]["manifests"]["verdict"] == "INFRA" and probe(fact, "download.manifest")["verdict"] == "FAIL"
    assert fact["verdict"] == "INFRA" and fact["manifests"] == []


def test_a_signature_that_cannot_be_downloaded_is_infra_and_unverified(signer):
    fa = sample(key=signer["key"])
    fa.fail[MAN + ".sig"] = 500
    fact = run(fa, **trusted_kw(signer))
    assert fact["sections"]["manifests"]["verdict"] == "INFRA"
    assert fact["manifests"][0]["signature"] == "unverified" and fact["manifests"][0]["trusted"] is False


def test_releases_store_column_says_not_in_store(stored, signer):
    fa = sample(key=signer["key"])
    fa.items = [i for i in fa.items if i["name"] != "a.gds.zst"]
    cfg = stored(fa)
    assert V.release_rows(cfg)[("demo-si", "1.0.0")]["store"][:2] == ("NOT IN STORE", "c-fail")


def test_a_stream_at_another_path_than_the_manifest_names_is_a_mismatch(stored, signer):
    fa = sample(key=signer["key"])
    gds = {"raw_md5": RAW_A, "store_path": "asic-candidate/chipa/blk/run2/stream/a.gds.zst"}
    with_manifest(fa, manifest_body(gds=gds), signer)
    cfg = stored(fa)
    assert V.taped_out(cfg, "demo-si")["rows"][0]["store"][:2] == ("MISMATCH", "c-fail")
    f = next(x for x in V.findings(cfg) if x["kind"] == "store_mismatch")
    assert f["evidence"] == ["the manifest names asic-candidate/chipa/blk/run2/stream/a.gds.zst with raw md5 %s; "
                             "the store holds no file there" % RAW_A]


def test_a_manifest_without_a_gds_md5_is_no_md5(stored, signer):
    fa = sample(key=signer["key"])
    with_manifest(fa, manifest_body(gds={"store_path": "asic-candidate/chipa/blk/run1/stream/a.gds.zst"}), signer)
    cfg = stored(fa)
    assert V.taped_out(cfg, "demo-si")["rows"][0]["store"][:2] == ("NO MD5", "c-warn")


def _artefact_manifest(fa, signer, **art) -> None:
    a = {"role": "stream", "path": "asic-candidate/chipa/blk/run1/stream/a.gds.zst"}
    a.update(art)
    with_manifest(fa, manifest_body(artefacts=[a]), signer)


def test_a_stored_md5_the_store_does_not_hold_is_a_mismatch(stored, signer):
    fa = sample(key=signer["key"])
    _artefact_manifest(fa, signer, stored_md5="e" * 32, raw_md5=RAW_A)
    f = [x for x in V.findings(stored(fa)) if x["kind"] == "store_mismatch"]
    assert len(f) == 1 and f[0]["evidence"] == ["asic-candidate/chipa/blk/run1/stream/a.gds.zst: manifest says stored "
                                                "md5 %s, the store %s" % ("e" * 32, ZST_A)]


def test_a_raw_md5_the_store_does_not_record_is_a_mismatch(stored, signer):
    fa = sample(key=signer["key"])
    _artefact_manifest(fa, signer, raw_md5="e" * 32)
    f = [x for x in V.findings(stored(fa)) if x["kind"] == "store_mismatch"]
    assert len(f) == 1 and f[0]["evidence"] == ["asic-candidate/chipa/blk/run1/stream/a.gds.zst: manifest says raw "
                                                "md5 %s, the store %s" % ("e" * 32, RAW_A)]


def test_matching_artefacts_raise_no_card(stored, signer):
    fa = sample(key=signer["key"])
    _artefact_manifest(fa, signer, stored_md5=ZST_A, raw_md5=RAW_A)
    assert [x for x in V.findings(stored(fa)) if x["kind"] == "store_mismatch"] == []


def test_a_release_lock_that_disagrees_is_a_mismatch(stored):
    cfg = stored(lock=[{"version": "1.0.0", "md5": "e" * 32, "bytes": 1}])
    f = [x for x in V.findings(cfg) if x["kind"] == "store_mismatch"]
    assert len(f) == 1 and f[0]["evidence"] == ["release lock 1.0.0 says GDS md5 %s, the manifest %s" % ("e" * 32, RAW_A)]


def test_no_release_not_archived_card_when_the_manifests_were_not_read(stored, signer):
    cfg = stored(sample(key=signer["key"], aql=False), records=[silicon_record(), silicon_record("other-si", "f" * 40)])
    assert [f for f in V.findings(cfg) if f["kind"] == "release_not_archived"] == []


def test_a_symlink_in_the_cache_is_never_served(stored):
    cfg = stored()
    sha1 = hashlib.sha1(b"# Errata\n\nnone yet\n").hexdigest()
    p = cfg.data_dir / I.CACHE_DIR / sha1
    assert V.cached_file(cfg, sha1) is not None
    target = cfg.data_dir / "elsewhere"
    target.write_bytes(p.read_bytes())
    p.unlink()
    p.symlink_to(target)
    assert V.cached_file(cfg, sha1) is None


def test_a_stream_without_a_raw_md5_never_joins_by_its_stored_checksum(stored, signer):
    fa = sample(key=signer["key"])
    fa.items = [i for i in fa.items if i["name"] != "a.gds.zst"]
    fa.items.append(item("asic-candidate", "chipa/blk/run1/stream", "a.gds.zst", size=100, md5=RAW_A))
    cfg = stored(fa)
    a = next(x for x in V.shown(cfg)[0]["fact"]["artefacts"] if x["name"] == "a.gds.zst")
    assert a["raw_md5"] is None and a["md5"] == RAW_A
    assert V.taped_out(cfg, "demo-si")["rows"][0]["store"][0] == "NOT IN STORE"
    assert V.release_rows(cfg)[("demo-si", "1.0.0")]["store"][0] == "NOT IN STORE"


# ---------------------------------------------------------------------------------------------------------------
# SI-3: encoded and Tomcat-style dot segments; SI-4: a signature at the repository root
# ---------------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("method, path", [
    ("PUT", "asic-release/..;/asic-candidate/x.gds"), ("PUT", "asic-release/%2e%2e/asic-candidate/x.gds"),
    ("PUT", "asic-release/%2E%2E/api/security/token"), ("PUT", "asic-release/.%2e/x"),
    ("PUT", "asic-release/x;jsessionid=1/y"), ("PUT", "asic-release//x"), ("PUT", "asic-release/%252e%252e/x"),
    ("GET", "api//security/token"), ("GET", "x/..;/api/security/token"), ("GET", "API/Security/token"),
    ("GET", "%61ccess/api/v1/tokens"), ("GET", "api/%73ecurity/token"),
])
def test_encoded_and_tomcat_dot_segments_are_refused(method, path):
    with pytest.raises(C.StoreRefused):
        C.check_request(method, path, publish_repo="asic-release")


def test_ordinary_encoded_names_still_pass():
    C.check_request("GET", "asic-record/p/b/docs/My%20Errata.md")
    C.check_request("PUT", "asic-release/chipa/1.0.0/release-manifest.json", publish_repo="asic-release")


def test_a_signed_manifest_at_the_repository_root_is_seen(signer):
    fa = sample(key=signer["key"])
    body, sig = fa.files[MAN], fa.files[MAN + ".sig"]
    fa.items = [i for i in fa.items if i["repo"] != "asic-release"]
    fa.items += [item("asic-release", ".", "tapeout-demo.json", size=len(body), sha1=hashlib.sha1(body).hexdigest()),
                 item("asic-release", ".", "tapeout-demo.json.sig")]
    fa.files["asic-release/tapeout-demo.json"], fa.files["asic-release/tapeout-demo.json.sig"] = body, sig
    m = run(fa, **trusted_kw(signer))["manifests"][0]
    assert (m["path"], m["signature"]) == ("tapeout-demo.json", "verified")


def test_settings_read_the_trust_keys(tmp_path, signer):
    cfg = load_config(write_cfg(tmp_path, URL, extra='allowed_signers = "%s"\nshared_logins = ["soclabs"]\n'
                                                       'signer_identity = "rm"\n' % signer["allowed"]))
    s = C.settings(cfg)
    assert (s.allowed_signers, s.shared_logins, s.signer_identity) == (signer["allowed"], ("soclabs",), "rm")
