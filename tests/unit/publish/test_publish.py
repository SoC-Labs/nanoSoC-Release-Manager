"""The publish unit (CR-ART-publish; G8, D12, G3): signed release manifests and tapeout records in the release store.

Hermetic: the releases tests' Scene (local git hosts, a registry, mirrors) makes a real, ready release draft; a
threaded fake Artifactory (fake_artifactory.py, shapes measured read-only on the lab's OSS 7.161.17) is the store.
No test reaches the lab's store. Each check named in the CR has a test that fails when the check is removed
(mutations listed in the Wave E publish report).
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parents[0] / "analysis"))
from analysis_fixtures import load_at  # noqa: E402
from fake_artifactory import FakeArtifactory  # noqa: E402

from nanosoc_rm import cli, contracts, db, jobs  # noqa: E402
from nanosoc_rm.config import ConfigError, load_config  # noqa: E402
from nanosoc_rm.publish import client as C  # noqa: E402
from nanosoc_rm.publish import service as S  # noqa: E402
from nanosoc_rm.publish import signing as SG  # noqa: E402
from nanosoc_rm.publish.core import PublishError, PublishRefused, PublishUsage, readiness  # noqa: E402

pytestmark = [pytest.mark.requirement("G8"), pytest.mark.requirement("D12")]


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


R = _load("rm_releases_tests_for_publish", HERE.parents[0] / "releases" / "test_releases.py")


def _keygen(path: Path, kind: str = "rsa") -> Path:
    args = ["ssh-keygen", "-q", "-N", "", "-C", "nanosoc-rm-test", "-f", str(path)]
    args += ["-t", "rsa", "-b", "2048"] if kind == "rsa" else ["-t", kind]
    subprocess.run(args, check=True, capture_output=True)
    return path


@pytest.fixture(scope="module")
def at_pins():
    return load_at("test_pins")


@pytest.fixture
def fake():
    f = FakeArtifactory()
    yield f
    f.close()


class World:
    """A ready release draft soc-v1 1.0.0 (silicon demo r0p0) and a config pointing at the fake store."""

    def __init__(self, scene, fake, tmp_path, monkeypatch, **store):
        self.scene, self.fake, self.tmp = scene, fake, tmp_path
        secrets = tmp_path / "secrets"
        secrets.mkdir()
        tok = secrets / "artifactory.token"
        tok.write_text(fake.token + "\n")
        tok.chmod(0o600)
        self.key = _keygen(secrets / "signing.key")
        table = {"url": fake.url, "token_file": str(tok), "signing_key": str(self.key), "publishers": ["alice"],
                 "shared_logins": ["soclabs"], "projects": {"soc": "demochip"}}
        table.update(store)
        lines = ["", "[roles]", 'default = ["member"]', "", "[stores.artifactory]"]
        for k, v in table.items():
            if v is None:
                continue
            if isinstance(v, dict):
                lines.append("%s = { %s }" % (k, ", ".join('%s = "%s"' % kv for kv in v.items())))
            else:
                lines.append("%s = %s" % (k, json.dumps(v)))
        with open(scene.env.config, "a") as fh:
            fh.write("\n".join(lines) + "\n")
        self.config = load_config(scene.env.config)
        self.lock_md5 = self.draft(monkeypatch)

    def draft(self, monkeypatch):
        import nanosoc_rm.releases.checklist as CL
        D = R.D
        monkeypatch.setattr(CL, "tier_checks", lambda *a: ([D.check("tier.%s" % t, "PASS", "stored") for t in
                                                            ("t0", "t1", "t2")] + [D.check("tier.t6", "UNVERIFIED",
                                                                                           "person", person=True)], []))
        monkeypatch.setattr(CL, "clone_checks", lambda *a: [D.check("clone.clean", "PASS", "stored")])
        arc = R._archive(self.tmp, R.RTL)
        self.gds = self.tmp / "chip.gds"
        self.gds.write_bytes(b"GDS" * 100)
        net = self.tmp / "chip.v"
        net.write_text("module chip; endmodule\n")
        doc = D.draft("soc-v1", "1.0.0", archive=arc, gds=self.gds, netlist=net,
                      waivers=[D.parse_waiver("*=taped out before the scheme")],
                      out=D.draft_path(self.config, "soc-v1", "1.0.0"))
        assert doc["fact"]["ready"], [c for c in doc["fact"]["checks"] if c["verdict"] not in ("PASS", "WAIVED")]
        self.archive_md5 = doc["fact"]["archive"]["md5"]
        return hashlib.md5(self.gds.read_bytes()).hexdigest()


@pytest.fixture
def world(at_pins, fake, tmp_path, monkeypatch):
    for k in ("RM_SCAN_RULES", "RM_SCAN_TOOLKIT"):
        monkeypatch.delenv(k, raising=False)
    scene = R.Scene(at_pins, tmp_path / "w")
    with scene.env.active():
        yield World(scene, fake, tmp_path, monkeypatch)
    scene.h.api.close()


KEY = "soc-v1/1.0.0"
FOLDER = "asic-release/demochip/1.0.0"


# ---------------------------------------------------------------------------------------------------------
# Signing
# ---------------------------------------------------------------------------------------------------------
def test_builtin_sshsig_signs_and_verifies_and_refuses_what_it_must(tmp_path):
    key = _keygen(tmp_path / "k")
    data = b'{"x": 1}\n'
    sig = SG.sign(data, key, ssh_keygen="/nonexistent/ssh-keygen")
    assert sig.backend == "builtin" and sig.text.startswith("-----BEGIN SSH SIGNATURE-----\n")
    assert all(len(ln) <= 70 for ln in sig.text.splitlines())
    allowed = SG.load_allowed(str(key) + ".pub")
    fp = subprocess.run(["ssh-keygen", "-l", "-f", str(key) + ".pub"], capture_output=True, text=True).stdout.split()[1]
    assert SG.verify(data, sig.text, allowed) == sig.fingerprint == fp
    assert SG.sign(data, key, ssh_keygen="/nonexistent").text == sig.text, "RSA PKCS#1 v1.5 is deterministic"
    with pytest.raises(SG.SignatureError, match="does not match"):
        SG.verify(data + b" ", sig.text, allowed)
    with pytest.raises(SG.SignatureError, match="namespace"):
        SG.verify(data, SG.sign(data, key, ssh_keygen="/x", namespace="other").text, allowed)
    other = _keygen(tmp_path / "k2")
    with pytest.raises(SG.SignatureError, match="not an allowed signer"):
        SG.verify(data, sig.text, SG.load_allowed(str(other) + ".pub"))
    # an allowed_signers line with a namespaces option that excludes nanosoc-rm does not accept it
    pub = (Path(str(key) + ".pub").read_text().split()[:2])
    (tmp_path / "allowed").write_text('rm namespaces="git" %s %s\n' % tuple(pub))
    with pytest.raises(SG.SignatureError, match="not an allowed signer"):
        SG.verify(data, sig.text, SG.load_allowed(tmp_path / "allowed"))
    (tmp_path / "allowed").write_text('rm namespaces="git,nanosoc-rm" %s %s\n' % tuple(pub))
    assert SG.verify(data, sig.text, SG.load_allowed(tmp_path / "allowed"), principal="rm") == fp
    # PEM (PKCS#1) keys sign the same way; an encrypted key and an ed25519 key without ssh-keygen -Y cannot
    pem = tmp_path / "pem"
    subprocess.run(["ssh-keygen", "-q", "-t", "rsa", "-b", "2048", "-m", "PEM", "-N", "", "-f", str(pem)], check=True)
    assert SG.verify(data, SG.sign(data, pem, ssh_keygen="/x").text, SG.load_allowed(str(pem) + ".pub"))
    enc = tmp_path / "enc"
    subprocess.run(["ssh-keygen", "-q", "-t", "rsa", "-b", "2048", "-N", "secret-pw", "-f", str(enc)], check=True)
    assert SG.backend(enc, "/x")[0] is None and "encrypted" in SG.backend(enc, "/x")[1]
    ed = _keygen(tmp_path / "ed", "ed25519")
    which, why = SG.backend(ed, "/x")
    assert which is None and "ssh-ed25519" in why and "8.1" in why


def test_ssh_keygen_backend_runs_only_dash_y(tmp_path, monkeypatch):
    """A fake OpenSSH 8.1+ ssh-keygen: the module runs `-Y sign` with the key and namespace, and refuses any other
    argv before starting a process."""
    log = tmp_path / "argv.log"
    shim = tmp_path / "ssh-keygen"
    key = _keygen(tmp_path / "k")
    real = SG.sign(b"payload", key, ssh_keygen="/x").text
    shim.write_text("#!/bin/sh\necho \"$@\" >> %s\nif [ \"$2\" = sign ] && [ $# -gt 2 ]; then\n"
                    "  for f; do :; done; cat > \"$f.sig\" <<'EOF'\n%sEOF\nfi\nexit 0\n" % (log, real))
    shim.chmod(0o755)
    SG.ssh_keygen_supports_y.cache_clear()
    sig = SG.sign(b"payload", key, ssh_keygen=str(shim))
    assert sig.backend == "ssh-keygen" and sig.text == real
    calls = log.read_text().splitlines()
    assert calls[0] == "-Y sign" and calls[1].startswith("-Y sign -f %s -n nanosoc-rm " % key)
    with pytest.raises(SG.SignatureError, match="refused"):
        SG._run([str(shim), "-y", "-f", str(key)])
    assert len(log.read_text().splitlines()) == 2
    SG.ssh_keygen_supports_y.cache_clear()


# ---------------------------------------------------------------------------------------------------------
# The client's allow-list
# ---------------------------------------------------------------------------------------------------------
def test_the_client_refuses_every_call_off_its_allow_list_before_any_io(fake):
    s = C.Store(fake.url, fake.token, release_repo="asic-release")
    refused = [("PUT", "asic-record/eth/x.json"), ("PUT", "asic-release/p/1.0.0/sub/x.json"),
               ("PUT", "asic-release/p/1.0.0/x.md5"), ("PUT", "asic-release/p/1.0.0/x.sha256"),
               ("POST", "api/search/aql"), ("POST", "api/build/promote/a"), ("DELETE", "asic-release/p/1.0.0/x"),
               ("PATCH", "api/build"), ("GET", "../access/api/v1/tokens"), ("PUT", "asic-release/p/../x/y"),
               ("GET", "asic-release//x"), ("PUT", "api/storage/asic-release/p/1.0.0/x.json"),
               # PUB-REV-2a: a `..` segment past the first (httpx would resolve it to <host>/access/api/...)
               ("GET", "asic-release/../../access/api/v1/tokens"), ("HEAD", "api/storage/../../access/api/v1/x"),
               # PUB-REV-1: `api/build` is a build-info record, only through put_build (which checks the name)
               ("PUT", "api/build")]
    for method, rel in refused:
        with pytest.raises(C.StoreRefused):
            s.check(method, rel)
    with pytest.raises(C.StoreRefused):
        s.promote("rm-release-p", "1.0.0", {"status": "RELEASED", "targetRepo": "asic-record"})
    with pytest.raises(C.StoreRefused):
        s.put_file("asic-record/x/y/z.json", b"x")
    # PUB-REV-1: put_file can never write a build record (a producer's build would be overwritten)
    producer = json.dumps({"version": "1.0.1", "name": "nanosoc-ethernet-chiplet", "number": "rc4",
                           "started": "2026-01-01T00:00:00.000+0000", "modules": []}).encode()
    with pytest.raises(C.StoreRefused):
        s.put_file("api/build", producer)
    # PUB-REV-5: credentials in the url would make httpx send Basic auth instead of the Bearer token
    for url in ("http://admin:pw@127.0.0.1:8082/artifactory", "http://admin@127.0.0.1:8082/artifactory"):
        with pytest.raises(C.StoreRefused, match="credentials"):
            C.Store(url, "t", release_repo="asic-release")
    for url in ("http://h:8082/access", "http://h:8082/artifactory/../access", "ftp://h/artifactory"):
        with pytest.raises(C.StoreRefused):
            C.Store(url, "t", release_repo="asic-release")
    assert fake.requests == [], "a refused call reached the store"
    assert s.check("PUT", "asic-release/p/1.0.0/release-manifest.json").endswith("/release-manifest.json")
    assert s.check("POST", "api/build/promote/rm-release-p/1.0.0")
    # PUB-R4: only the tool's own builds are written or promoted, and a promotion body only sets a status
    for rel in ("api/build/promote/nanosoc-ethernet-chiplet/rc4", "api/build/promote/rm-release-/1",
                "api/build/promote/someone-elses-build/1"):
        with pytest.raises(C.StoreRefused, match="did not write"):
            s.check("POST", rel)
    for body in ({"status": "X", "properties": {"evil": ["1"]}}, {"status": "X", "dependencies": True},
                 {"status": "X", "scopes": ["compile"]}, {"comment": "no status"}):
        with pytest.raises(C.StoreRefused, match="only sets a status"):
            s.promote("rm-release-p", "1.0.0", body)
    with pytest.raises(C.StoreRefused, match="not one of this tool's records"):
        s.put_build({"name": "nanosoc-ethernet-chiplet", "number": "rc4"})
    assert ("nanosoc-ethernet-chiplet", "rc4") not in fake.builds
    from nanosoc_rm.publish.core import BUILD_PREFIX
    assert set(BUILD_PREFIX.values()) == set(C.BUILD_NAME_PREFIXES)
    assert fake.requests == [], "a refused build call reached the store"
    # the token goes as a Bearer header only, never Basic
    s.head("asic-release/p/1.0.0/none.json")
    assert fake.requests == [("HEAD", "/artifactory/asic-release/p/1.0.0/none.json", "bearer")] and fake.basic == 0
    # the web process refuses every write
    try:
        C.refuse_writes_here("the web process")
        with pytest.raises(C.StoreRefused, match="web process"):
            s.check("PUT", "asic-release/p/1.0.0/release-manifest.json")
        with pytest.raises(C.StoreRefused, match="web process"):
            s.check("POST", "api/build/promote/rm-release-p/1.0.0")
        assert s.check("GET", "api/build/x/1")
    finally:
        C.refuse_writes_here(None)
    s.close()


def test_config_keys_refuse_a_shared_publisher_and_a_url_off_artifactory(tmp_path):
    base = '[service]\ndata_dir = "%s"\n[stores.artifactory]\n' % tmp_path
    p = tmp_path / "c.toml"
    p.write_text(base + 'url = "http://localhost:8082/artifactory"\npublishers = ["alice", "soclabs"]\n'
                        'shared_logins = ["soclabs"]\n')
    with pytest.raises(ConfigError, match="soclabs is a shared login"):
        load_config(p)
    p.write_text(base + 'url = "http://localhost:8082/access"\n')
    with pytest.raises(ConfigError, match="must be http"):
        load_config(p)
    p.write_text(base + 'url = "http://admin:pw@localhost:8082/artifactory"\n')     # PUB-REV-5
    with pytest.raises(ConfigError, match="credentials"):
        load_config(p)
    p.write_text(base + 'url = "http://localhost:8082/artifactory"\ntoken_file = "tok"\ntoken_expires = "2027-10-14"\n')
    assert load_config(p)["stores"]["artifactory"]["token_file"] == str(tmp_path / "tok")


# ---------------------------------------------------------------------------------------------------------
# Release: preview, publish, read back, ledger
# ---------------------------------------------------------------------------------------------------------
def test_publish_release_end_to_end(world):
    fake, cfg = world.fake, world.config
    assert readiness(cfg) == []
    prev = S.preview(cfg, "release", KEY, by="alice")
    assert prev["problems"] == [] and {t["state"] for t in prev["targets"]} == {"absent"}
    assert prev["store_path"] == FOLDER + "/release-manifest.json"
    assert prev["diff"] == ["new: nothing of this project was published by this tool before"]
    assert fake.writes() == [], "a preview wrote to the store"
    rec = prev["record"]
    assert contracts.validate("release-manifest", rec) == []
    assert rec["commit"] == world.scene.s1 and rec["outputs"]["gds"]["md5"] == world.lock_md5
    assert {p["tree_path"]: p["commit"] for p in rec["pins"]} == {"ip": world.scene.i1, "gen": world.scene.g1}
    assert {d["name"] for d in rec["documents"]} >= {"release.lock.yaml", "ERRATA.md", "GATES.md"}
    obs = S.publish(cfg, "release", KEY, confirm=prev["sha256"], by="alice")
    assert contracts.validate_observation(obs) == []
    f = obs["fact"]
    assert f["outcome"] == "published" and f["verified"] == {"checksums": True, "signature": True, "build": True}
    assert f["published_by"] == "alice" and f["build"] == {"name": "rm-release-demochip", "number": "1.0.0",
                                                           "status": "RELEASED"}
    stored = fake.files[FOLDER + "/release-manifest.json"]
    assert hashlib.sha256(stored["data"]).hexdigest() == prev["sha256"] == f["manifest_sha256"]
    assert stored["props"]["rm.commit"] == [world.scene.s1] and stored["props"]["rm.repo"] == ["SoC-Labs/soc"]
    assert stored["props"]["rm.role"] == ["manifest"] and stored["props"]["rm.tag"] == ["v1.0.0"]
    sig = fake.files[FOLDER + "/release-manifest.json.sig"]["data"].decode()
    assert SG.verify(stored["data"], sig, SG.load_allowed(str(world.key) + ".pub"))
    assert not any(p.endswith(".tar.gz") for p in fake.files), "the private archive copy was uploaded"
    bi = fake.builds[("rm-release-demochip", "1.0.0")]
    assert [x["status"] for x in bi["statuses"]] == ["RELEASED"] and bi["properties"]["rm.manifest_sha256"] == f["manifest_sha256"]
    assert bi["vcs"][0]["revision"] == world.scene.s1 and len(bi["vcs"]) == 3
    assert fake.overwrites == 0 and fake.basic == 0
    assert db.latest("release_publication", config=cfg)[0]["subject"] == \
        "release_publication:demochip:1.0.0:release-manifest.json"
    acts = [a for a in db.actions(config=cfg) if a["action"] == "publish_release"]
    assert acts[-1]["login"] == "alice" and acts[-1]["result"].startswith("published sha256 ")
    # a retry finds the same bytes everywhere: nothing is uploaded again, nothing promoted twice
    n = len(fake.writes())
    again = S.publish(cfg, "release", KEY, confirm=prev["sha256"], by="alice")
    assert again["fact"]["outcome"] == "already-published" and len(fake.writes()) == n
    # a second preview diffs against the published copy (nothing changed)
    assert S.preview(cfg, "release", KEY)["problems"] == []


def test_publish_refuses_what_it_must_and_writes_nothing(world):
    fake, cfg = world.fake, world.config
    prev = S.preview(cfg, "release", KEY, by="alice")
    # a confirm for another record
    with pytest.raises(PublishRefused, match="not the confirmed"):
        S.publish(cfg, "release", KEY, confirm="0" * 64, by="alice")
    # a shared login, and a login not listed, never publish
    for who in ("soclabs", "mallory"):
        with pytest.raises(PublishRefused):
            S.publish(cfg, "release", KEY, confirm=prev["sha256"], by=who)
    # an existing path with other bytes: refused before ANY upload (HEAD first, never overwrite)
    fake.seed(FOLDER + "/ERRATA.md", b"someone else's errata\n")
    p2 = S.preview(cfg, "release", KEY)
    assert any("ERRATA.md already holds other bytes" in p for p in p2["problems"])
    assert {t["path"]: t["state"] for t in p2["targets"]}[FOLDER + "/ERRATA.md"] == "differs"
    with pytest.raises(PublishRefused, match="nothing uploaded"):
        S.publish(cfg, "release", KEY, confirm=prev["sha256"], by="alice")
    assert fake.writes() == [] and fake.overwrites == 0
    refusals = [a for a in db.actions(config=cfg) if a["result"].startswith("refused")]
    assert len(refusals) == 4 and {a["login"] for a in refusals} == {"alice", "soclabs", "mallory"}


def test_an_existing_build_for_another_record_is_refused_before_upload(world):
    fake, cfg = world.fake, world.config
    prev = S.preview(cfg, "release", KEY, by="alice")
    fake.builds[("rm-release-demochip", "1.0.0")] = {"name": "rm-release-demochip", "number": "1.0.0",
                                                      "properties": {"rm.manifest_sha256": "0" * 64}}
    assert any("already exists for another record" in p for p in S.preview(cfg, "release", KEY)["problems"])
    with pytest.raises(PublishRefused, match="nothing uploaded: build rm-release-demochip/1.0.0 already exists"):
        S.publish(cfg, "release", KEY, confirm=prev["sha256"], by="alice")
    assert fake.writes() == []


def test_readiness_refuses_open_secret_files_and_an_expired_token(world):
    import datetime as dt
    cfg = world.config
    assert readiness(cfg) == []
    for key in ("token_file", "signing_key"):
        f = Path(cfg["stores"]["artifactory"][key])
        f.chmod(0o644)
        try:
            assert any("readable by others" in p and "chmod 600" in p for p in readiness(cfg)), key
        finally:
            f.chmod(0o600)
    with open(world.scene.env.config, "a") as fh:
        fh.write('token_expires = "2026-06-30"\n')
    cfg = load_config(world.scene.env.config)
    assert readiness(cfg, today=dt.date(2026, 6, 30)) == []
    assert readiness(cfg, today=dt.date(2026, 7, 1)) == ["the store token expired on 2026-06-30: mint a new one"]


def test_read_back_and_signature_must_verify(world):
    fake, cfg = world.fake, world.config
    prev = S.preview(cfg, "release", KEY, by="alice")
    fake.serve_wrong.add(FOLDER + "/release-manifest.json")
    with pytest.raises(PublishRefused, match="read-back"):
        S.publish(cfg, "release", KEY, confirm=prev["sha256"], by="alice")
    assert db.latest("release_publication", config=cfg) == []


def test_every_uploaded_document_is_read_back_by_checksum(world):
    """PUB-REV-2c: a document the store keeps wrong (only the record and its .sig are compared byte for byte) is
    caught by the stored sha256 of every uploaded path."""
    fake, cfg = world.fake, world.config
    prev = S.preview(cfg, "release", KEY, by="alice")
    fake.store_wrong.add(FOLDER + "/ERRATA.md")
    with pytest.raises(PublishRefused, match="read-back: %s/ERRATA.md is different" % FOLDER):
        S.publish(cfg, "release", KEY, confirm=prev["sha256"], by="alice")
    assert db.latest("release_publication", config=cfg) == []


def test_a_document_edited_after_the_draft_is_refused(world):
    """PUB-REV-2d: ERRATA.md edited after the draft was reviewed is never published silently."""
    d = R.D.draft_path(world.config, "soc-v1", "1.0.0")
    with open(d / "ERRATA.md", "a") as fh:
        fh.write("\nan unreviewed line\n")
    with pytest.raises(PublishRefused, match="ERRATA.md changed since the draft was written"):
        S.preview(world.config, "release", KEY)
    assert world.fake.requests == []


def test_the_signer_identity_is_enforced_with_an_allowed_signers_file(world):
    """PUB-REV-4: a key listed only under another principal is not the configured signer_identity, as
    `ssh-keygen -Y verify -I nanosoc-rm` would also say."""
    fake = world.fake
    prev = S.preview(world.config, "release", KEY, by="alice")
    pub = (Path(str(world.key) + ".pub")).read_text().split()
    allowed = world.tmp / "allowed_signers"
    allowed.write_text('mallory namespaces="nanosoc-rm" %s %s\n' % (pub[0], pub[1]))
    with open(world.scene.env.config, "a") as fh:
        fh.write('allowed_signers = "%s"\nsigner_identity = "nanosoc-rm"\n' % allowed)
    with pytest.raises(SG.SignatureError, match="not an allowed signer"):
        S.publish(load_config(world.scene.env.config), "release", KEY, confirm=prev["sha256"], by="alice")
    assert fake.writes() == []
    allowed.write_text('nanosoc-rm namespaces="nanosoc-rm" %s %s\n' % (pub[0], pub[1]))
    obs = S.publish(load_config(world.scene.env.config), "release", KEY, confirm=prev["sha256"], by="alice")
    assert obs["fact"]["verified"]["signature"] is True


def test_serve_refuses_store_writes_in_the_web_process(world, monkeypatch):
    """PUB-REV-2b: `nanosoc-rm serve` turns the client's write refusal on before it serves anything."""
    import uvicorn
    seen = []
    monkeypatch.setattr(uvicorn, "run", lambda *a, **k: seen.append(C.writes_refused()))
    try:
        r = CliRunner().invoke(cli.main, ["serve", "--no-schedule", "--no-worker"])
        assert r.exit_code == 0, r.output
        assert seen == ["the web process"]
    finally:
        C.refuse_writes_here(None)


def test_a_signature_that_does_not_verify_is_refused_before_any_upload(world):
    """PUB-R1: the store is never overwritten, so the signature is verified against the allowed keys before the
    first PUT; otherwise an unverifiable .sig would stay in the folder and block every later publish."""
    fake, cfg = world.fake, world.config
    prev = S.preview(cfg, "release", KEY, by="alice")
    other = _keygen(world.tmp / "other")
    with open(world.scene.env.config, "a") as fh:
        fh.write('allowed_signers = "%s.pub"\n' % other)
    with pytest.raises(SG.SignatureError, match="not an allowed signer"):
        S.publish(load_config(world.scene.env.config), "release", KEY, confirm=prev["sha256"], by="alice")
    # PUB-R1: refused BEFORE the first upload, so no unverifiable .sig is left in a folder that is never overwritten
    assert fake.writes() == [], "a signature that does not verify was uploaded"
    assert not any(p.endswith(".sig") for p in fake.files)
    assert db.latest("release_publication", config=cfg) == []
    # signing with the allowed key then publishes into the same, still clean folder
    text = Path(world.scene.env.config).read_text()
    Path(world.scene.env.config).write_text(text.replace('signing_key = "%s"' % world.key, 'signing_key = "%s"' % other))
    cfg2 = load_config(world.scene.env.config)
    assert S.publish(cfg2, "release", KEY, confirm=prev["sha256"], by="alice")["fact"]["outcome"] == "published"


def test_a_draft_that_is_not_ready_is_refused(world, monkeypatch):
    d = R.D.draft_path(world.config, "soc-v1", "1.0.0")
    doc = json.loads((d / "draft.json").read_text())
    doc["fact"]["ready"] = False
    (d / "draft.json").write_text(json.dumps(doc))
    with pytest.raises(PublishRefused, match="not ready"):
        S.preview(world.config, "release", KEY)
    with pytest.raises(PublishUsage):
        S.preview(world.config, "release", "soc-v1/9.9.9")


def test_a_referenced_artefact_joins_by_raw_md5(world):
    fake, cfg = world.fake, world.config
    import zlib
    gz = zlib.compress(world.gds.read_bytes())
    fake.seed("asic-candidate/demochip/run1/stream/chip.gds.zst", gz, {"pnr.raw_md5": world.lock_md5})
    fake.seed("asic-record/demochip/run1/source/build_soc.tar.gz", (world.tmp / "build_soc.tar.gz").read_bytes())
    arts = [("gds", "asic-candidate/demochip/run1/stream/chip.gds.zst"),
            ("archive", "asic-record/demochip/run1/source/build_soc.tar.gz")]
    rec = S.preview(cfg, "release", KEY, artefacts=arts)["record"]
    got = {a["role"]: a for a in rec["artefacts"]}
    assert got["gds"]["raw_md5"] == world.lock_md5 and got["gds"]["raw_md5_from"] == "property"
    assert got["gds"]["stored_md5"] == hashlib.md5(gz).hexdigest() != world.lock_md5
    assert got["archive"]["raw_md5_from"] == "lock" and got["archive"]["raw_md5"] == world.archive_md5
    # a stream whose raw-md5 property names other bytes is refused: never joined by the compressed checksum
    fake.seed("asic-candidate/demochip/run2/stream/chip.gds.zst", gz, {"pnr.raw_md5": "f" * 32})
    with pytest.raises(PublishRefused, match="matches neither"):
        S.preview(cfg, "release", KEY, artefacts=[("gds", "asic-candidate/demochip/run2/stream/chip.gds.zst")])
    with pytest.raises(PublishRefused, match="not in the store"):
        S.preview(cfg, "release", KEY, artefacts=[("logs", "asic-record/nothing/here.txt")])
    # a compressed artefact the lock does not name and with no raw-md5 property: its stored (compressed) md5 is
    # NEVER recorded as its raw md5; an uncompressed one's stored md5 is its raw md5
    fake.seed("asic-record/demochip/run1/logs/run.log.zst", gz)
    fake.seed("asic-record/demochip/run1/logs/run.log", b"log text\n")
    rec = S.preview(cfg, "release", KEY, artefacts=[("logs", "asic-record/demochip/run1/logs/run.log.zst"),
                                                     ("other", "asic-record/demochip/run1/logs/run.log")])["record"]
    got = {a["role"]: a for a in rec["artefacts"]}
    assert got["logs"]["raw_md5"] is None and got["logs"]["raw_md5_from"] is None
    assert got["other"]["raw_md5"] == hashlib.md5(b"log text\n").hexdigest() and got["other"]["raw_md5_from"] == "stored"
    assert fake.writes() == []


# ---------------------------------------------------------------------------------------------------------
# Tapeout
# ---------------------------------------------------------------------------------------------------------
def _tapeout_draft(world, status="accepted", raw=None, gds_extra=None, **extra):
    data = world.gds.read_bytes()
    raw = raw or hashlib.md5(data).hexdigest()
    import zlib
    world.fake.seed("asic-candidate/demochip/run1/stream/chip.gds.zst", zlib.compress(data),
                    {"pnr.raw_md5": hashlib.md5(data).hexdigest(), "layout.canonical_hash": "a" * 64})
    rec = {"tapeout_id": "demochip-r0", "project": "demochip", "block": "demo_pads", "chip": "demo",
           "silicon_revision": "r0p0", "configuration": "soc-v1", "status": status,
           "tapeout_status": "free text the backfill wrote",
           "release": {"project": "demochip", "version": "1.0.0"},
           "foundry": {"foundry": "ExampleFab", "node": "nn", "shuttle": "MPW-0000"},
           "pdk": {"name": "example-pdk", "version": None}, "libraries": [{"name": "example-stdcells", "version": "1"}],
           "submission": {"submitted_on": "2026-01-02", "foundry_graded_on": "2026-01-05"},
           "source": {"repo": {"host": "github", "path": "SoC-Labs/soc"}, "tags": ["v1.0.0"],
                      "tag_commit": world.scene.s1, "pins": {"ip": world.scene.i1, "gen": world.scene.g1}},
           "gds": {"file": "chip.gds", "raw_md5": raw, "bytes": len(data), "canonical_sha256": "a" * 64,
                   "store_path": "asic-candidate/demochip/run1/stream/chip.gds.zst"},
           "signoff": {"evidence_report": "asic-record/demochip/run1/evidence_report.json"},
           "foundry_returns": [{"archive": "Archive_x", "cites_raw_md5": raw, "final_report_md5": "b" * 32}],
           "errata": {"configuration": "soc-v1", "ids": ["DEMO-R0P0-ERR-001"], "doc_revision": 1},
           "documents": [{"kind": "reference-manual", "store_path": "asic-record/demochip/run1/docs/m.tar.zst"}]}
    rec.update(extra)
    rec["gds"].update(gds_extra or {})
    if status is None:
        del rec["status"]                         # a backfill draft: only the free-text tapeout_status
    d = Path(world.config["registry"]["path"]) / "tapeouts"
    d.mkdir(exist_ok=True)
    (d / "demochip-r0.tapeout.yaml").write_text(yaml.safe_dump(
        {"schema": 1, "kind": "tapeout", "status": "draft", "record": rec, "sources": {}, "citations": []},
        sort_keys=False))


def test_register_tapeout_promotes_taped_out_once_submitted(world):
    fake, cfg = world.fake, world.config
    _tapeout_draft(world)
    prev = S.preview(cfg, "tapeout", "demochip-r0")
    rec = prev["record"]
    assert contracts.validate("tapeout-record", rec) == [] and prev["problems"] == []
    assert rec["status"] == "accepted" and rec["gds"]["raw_md5_from"] == "property"
    assert rec["gds"]["layout_hash"] == "a" * 64 and rec["submission_date"] == "2026-01-02"
    assert rec["foundry_verdict"]["verdict"] == "accepted" and rec["source"]["commit"] == world.scene.s1
    obs = S.publish(cfg, "tapeout", "demochip-r0", confirm=prev["sha256"], by="alice")
    f = obs["fact"]
    assert f["store_path"] == FOLDER + "/tapeout-demochip-r0.json" and f["build"]["status"] == "TAPED-OUT"
    assert [x["status"] for x in fake.builds[("rm-tapeout-demochip", "demochip-r0")]["statuses"]] == ["TAPED-OUT"]
    props = fake.files[FOLDER + "/tapeout-demochip-r0.json"]["props"]
    assert props["rm.tapeout_id"] == ["demochip-r0"] and props["rm.subject_md5"] == [rec["gds"]["raw_md5"]]


def test_a_draft_tapeout_is_not_promoted_and_bad_facts_are_refused(world):
    fake, cfg = world.fake, world.config
    _tapeout_draft(world, status="draft")
    prev = S.preview(cfg, "tapeout", "demochip-r0")
    S.publish(cfg, "tapeout", "demochip-r0", confirm=prev["sha256"], by="alice")
    assert "statuses" not in fake.builds[("rm-tapeout-demochip", "demochip-r0")]
    _tapeout_draft(world, raw="c" * 32)
    with pytest.raises(PublishRefused, match="raw md5"):
        S.preview(cfg, "tapeout", "demochip-r0")
    _tapeout_draft(world, status=None)
    with pytest.raises(PublishRefused, match="not one of draft"):
        S.preview(cfg, "tapeout", "demochip-r0")
    _tapeout_draft(world, pdk={"name": None, "version": None})
    with pytest.raises(PublishRefused, match="/pdk/name"):
        S.preview(cfg, "tapeout", "demochip-r0")
    # the stored GDS must agree with the draft on its layout hash and its stored sha256
    _tapeout_draft(world, gds_extra={"canonical_sha256": "d" * 64})
    with pytest.raises(PublishRefused, match="layout hash property"):
        S.preview(cfg, "tapeout", "demochip-r0")
    _tapeout_draft(world, gds_extra={"stored_sha256": "e" * 64})
    with pytest.raises(PublishRefused, match="stored GDS's sha256"):
        S.preview(cfg, "tapeout", "demochip-r0")
    # a foundry return that cites other bytes than the draft's GDS
    _tapeout_draft(world, foundry_returns=[{"archive": "Archive_x", "cites_raw_md5": "9" * 32}])
    with pytest.raises(PublishRefused, match="cites raw md5"):
        S.preview(cfg, "tapeout", "demochip-r0")


def test_a_tapeout_record_joins_its_own_release_folder(world):
    """PUB-R2: the tapeout record is filed in its release's folder; the draft's release project, the configuration's
    mapped project and the release manifest sha256 must agree with that folder and what is stored there."""
    fake, cfg = world.fake, world.config
    _tapeout_draft(world, release={"project": "otherchip", "version": "1.0.0", "manifest_sha256": "f" * 64})
    with pytest.raises(PublishRefused, match="must agree"):
        S.preview(cfg, "tapeout", "demochip-r0")
    _tapeout_draft(world, project="otherchip", release={"project": "otherchip", "version": "1.0.0"})
    with pytest.raises(PublishRefused, match="filed under project demochip"):
        S.preview(cfg, "tapeout", "demochip-r0")
    _tapeout_draft(world, release={"project": "demochip", "version": "1.0.0", "manifest_sha256": "f" * 64})
    with pytest.raises(PublishRefused, match="not in the store"):
        S.preview(cfg, "tapeout", "demochip-r0")
    # with no release published and none claimed, the record says so (null), and nothing was written
    _tapeout_draft(world)
    assert S.preview(cfg, "tapeout", "demochip-r0")["record"]["release"]["manifest_sha256"] is None
    assert fake.writes() == []
    # once the release is published, the record carries the STORED manifest's sha256, and a draft that claims
    # another one is refused
    rel = S.preview(cfg, "release", KEY, by="alice")
    S.publish(cfg, "release", KEY, confirm=rel["sha256"], by="alice")
    prev = S.preview(cfg, "tapeout", "demochip-r0")
    assert prev["problems"] == [] and prev["record"]["release"] == {"project": "demochip", "version": "1.0.0",
                                                                   "manifest_sha256": rel["sha256"]}
    _tapeout_draft(world, release={"project": "demochip", "version": "1.0.0", "manifest_sha256": "f" * 64})
    with pytest.raises(PublishRefused, match="the stored"):
        S.preview(cfg, "tapeout", "demochip-r0")


# ---------------------------------------------------------------------------------------------------------
# CLI and GUI
# ---------------------------------------------------------------------------------------------------------
def test_cli_needs_a_preview_then_publishes_exactly_it(world):
    r = CliRunner().invoke(cli.main, ["publish-release", KEY])
    assert r.exit_code == 3 and "--dry-run" in r.output
    r = CliRunner().invoke(cli.main, ["publish-release", KEY, "--dry-run", "--json"])
    assert r.exit_code == 0, r.output
    sha = json.loads(r.output.strip().splitlines()[-1])["sha256"]
    r = CliRunner().invoke(cli.main, ["publish-release", KEY, "--confirm", "1" * 64])
    assert r.exit_code == 1 and "not the confirmed" in r.output
    r = CliRunner().invoke(cli.main, ["publish-release", KEY, "--confirm", sha, "--by", "soclabs"])
    assert r.exit_code == 1 and "shared login" in r.output
    # PUB-REV-3: --by never claims a cli: login (that skips the publisher check): only the real one is cli:
    for fake_cli in ("cli:soclabs", "cli:" + ("x" if S.cli_login() != "cli:x" else "y")):
        r = CliRunner().invoke(cli.main, ["publish-release", KEY, "--confirm", sha, "--by", fake_cli])
        assert r.exit_code == 1 and "cli:" in r.output, r.output
    assert world.fake.writes() == []
    r = CliRunner().invoke(cli.main, ["publish-release", KEY, "--confirm", sha, "--json"])
    assert r.exit_code == 0, r.output
    assert json.loads(r.output.strip().splitlines()[-1])["fact"]["published_by"] == S.cli_login()
    # a --by for a listed publisher is recorded with the account that ran the command
    obs = S.publish(world.config, "release", KEY, confirm=sha, by="alice")
    assert obs["fact"]["published_by"] == "alice"
    assert obs["fact"]["run_by"] == S.cli_login() and contracts.validate_observation(obs) == []
    r = CliRunner().invoke(cli.main, ["register-tapeout", "no-such", "--dry-run"])
    assert r.exit_code == 3


def _client(world, login):
    from fastapi.testclient import TestClient
    from nanosoc_rm import auth
    from nanosoc_rm.web import create_app
    c = TestClient(create_app(world.scene.env.config, auth=auth.FakeMembership(login=login)))
    page = c.get("/publish/")
    csrf = page.text.split('"X-CSRF-Token": "', 1)[1].split('"', 1)[0]
    return c, csrf


def test_gui_preview_confirm_and_who_may_publish(world):
    fake, cfg = world.fake, world.config
    c, csrf = _client(world, "alice")
    idx = c.get("/publish/")
    assert idx.status_code == 200 and 'data-draft="soc-v1/1.0.0"' in idx.text and "data-none-published" in idx.text
    assert "data-publish-blocked" not in idx.text
    page = c.get("/publish/release/soc-v1/1.0.0")
    assert page.status_code == 200 and "data-preview-form" in page.text and "Publish appears once a preview" in page.text
    assert fake.requests == [], "a GET reached the store"
    # Preview: a POST queues a job; the job (a child CLI process) reads the store and writes the preview
    r = c.post("/publish/release/soc-v1/1.0.0/preview", data={"csrf_token": csrf}, follow_redirects=False)
    assert r.status_code == 303 and "job=" in r.headers["location"]
    job = jobs.jobs(cfg, kind="publish-release")[0]
    assert job["params"]["by"] == "alice" and job["params"]["confirm"] is None
    assert jobs.Worker(world.scene.env.config).step() == job["id"]
    assert jobs.get(cfg, job["id"])["state"] == "done", jobs.log_text(cfg, job["id"])
    assert fake.writes() == []
    page = c.get("/publish/release/soc-v1/1.0.0")
    sha = page.text.split('data-preview="', 1)[1].split('"', 1)[0]
    assert len(sha) == 64 and "data-confirm-form" in page.text and 'data-publish-button>Publish' in page.text
    # Confirm: needs the box ticked and the stored preview's sha256
    url = "/publish/release/soc-v1/1.0.0/confirm"
    assert c.post(url, data={"csrf_token": csrf, "sha256": sha}).status_code == 400
    assert c.post(url, data={"csrf_token": csrf, "sha256": "2" * 64, "ack": "1"}).status_code == 409
    r = c.post(url, data={"csrf_token": csrf, "sha256": sha, "ack": "1"}, follow_redirects=False)
    assert r.status_code == 303
    act = [a for a in db.actions(config=cfg) if a["action"] == "publish_release"]
    assert act and act[-1]["login"] == "alice" and sha in act[-1]["result"]
    job = jobs.jobs(cfg, kind="publish-release")[0]
    assert job["params"]["confirm"] == sha
    assert jobs.Worker(world.scene.env.config).step() == job["id"]
    assert jobs.get(cfg, job["id"])["state"] == "done", jobs.log_text(cfg, job["id"])
    assert FOLDER + "/release-manifest.json" in fake.files
    idx = c.get("/publish/")
    assert 'data-published="%s/release-manifest.json"' % FOLDER in idx.text
    assert "data-publish-link" in c.get("/releases/soc-v1/1.0.0").text
    # the shared login sees why it cannot publish, and its POSTs are refused
    s, s_csrf = _client(world, "soclabs")
    page = s.get("/publish/release/soc-v1/1.0.0")
    assert "data-publish-blocked" in page.text and "a shared login never publishes" in page.text
    assert "data-preview-form" not in page.text
    for path in ("/publish/release/soc-v1/1.0.0/preview", url):
        assert s.post(path, data={"csrf_token": s_csrf, "sha256": sha, "ack": "1"}).status_code == 403


def test_gui_is_disabled_with_a_reason_without_token_or_key(world):
    os.unlink(world.config["stores"]["artifactory"]["token_file"])
    c, csrf = _client(world, "alice")
    page = c.get("/publish/release/soc-v1/1.0.0")
    assert "data-publish-blocked" in page.text and "store token file does not exist" in page.text
    assert "data-preview-form" not in page.text
    r = c.post("/publish/release/soc-v1/1.0.0/confirm", data={"csrf_token": csrf, "sha256": "3" * 64, "ack": "1"})
    assert r.status_code == 409
    with pytest.raises(PublishError, match="token_file"):
        S.publish(world.config, "release", KEY, confirm="3" * 64, by="alice")


def test_gui_confirm_refuses_a_preview_with_problems(world):
    fake, cfg = world.fake, world.config
    fake.seed(FOLDER + "/ERRATA.md", b"someone else's errata\n")
    prev = S.preview(cfg, "release", KEY, by="alice")
    assert prev["problems"]
    c, csrf = _client(world, "alice")
    r = c.post("/publish/release/soc-v1/1.0.0/confirm", data={"csrf_token": csrf, "sha256": prev["sha256"], "ack": "1"})
    assert r.status_code == 409 and "found problems" in r.text
    assert jobs.jobs(cfg, kind="publish-release") == []


def test_gui_gets_never_read_the_private_key_nor_run_ssh_keygen(world, monkeypatch):
    """PUB-R5: the web process only stats the signing key; parsing it and running ssh-keygen belong to the job."""
    prev = S.preview(world.config, "release", KEY, by="alice")      # (the job's work, before the spies)
    world.fake.requests.clear()
    seen = []
    real_load, real_run = SG._load_private, SG._run
    monkeypatch.setattr(SG, "_load_private", lambda *a, **k: seen.append("parse") or real_load(*a, **k))
    monkeypatch.setattr(SG, "_run", lambda *a, **k: seen.append("ssh-keygen") or real_run(*a, **k))
    SG.ssh_keygen_supports_y.cache_clear()
    try:
        for who in ("bob", "alice"):
            c, csrf = _client(world, who)
            for url in ("/publish/", "/publish/release/soc-v1/1.0.0"):
                assert c.get(url).status_code == 200
        r = c.post("/publish/release/soc-v1/1.0.0/confirm",
                   data={"csrf_token": csrf, "sha256": prev["sha256"], "ack": "1"}, follow_redirects=False)
        assert r.status_code == 303, r.text
        assert seen == [], seen
        assert world.fake.requests == []
        # the job's own readiness still probes the signer
        assert readiness(world.config) == [] and seen
    finally:
        SG.ssh_keygen_supports_y.cache_clear()


def test_scheduler_reaching_the_publish_unit_is_soc_release_automation(tmp_path):
    """A7 rule 5b: a scheduler job that imports the publish package or queues its job kinds enables Phase 5's
    soc_release automation, whatever it is called; the real scheduler reaches neither."""
    from nanosoc_rm import phasegates
    from nanosoc_rm.schedule import JOBS
    assert "soc_release" not in phasegates.automation_reached(JOBS)

    def table(name, body):
        f = tmp_path / ("%s.py" % name)
        f.write_text("from nanosoc_rm.schedule import Job\n" + body)
        spec = importlib.util.spec_from_file_location(name, f)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod.JOBS
    direct = table("pub_direct", "def _go(c):\n    from nanosoc_rm.publish import publish\n"
                                 "    publish(c, 'release', 'x/1.0.0', confirm='0'*64)\n"
                                 'JOBS = (Job("poll", "poll", _go),)\n')
    assert "soc_release" in phasegates.automation_reached(direct)   # (it also reaches releases -> matrix code)
    queued = table("pub_queued", "def _go(c):\n    from nanosoc_rm import jobs\n"
                                 '    jobs.submit(c, "publish-release", {}, "nightly")\n'
                                 'JOBS = (Job("nightly_t0", "nightly", _go),)\n')
    assert set(phasegates.automation_reached(queued)) == {"soc_release"}


def test_the_publish_effect_is_confined_to_the_publish_unit():
    """AT-A2/A6 census (CR-ART-publish): the store writes are declared `publish`, guarded, in nanosoc_rm/publish/
    only; the v1 "exactly one write" (the CI trigger) is unchanged; a publish entry anywhere else is a problem."""
    from nanosoc_rm.contracts import WRITE_CALLS
    sw = _load("scope_web_for_publish", HERE.parents[1] / "acceptance" / "test_scope_web.py")
    assert sw.allowlist_problems(WRITE_CALLS) == []
    pub = [w for w in WRITE_CALLS if w["effect"] == "publish"]
    assert {(w["file"], w["method"]) for w in pub} == {("nanosoc_rm/publish/client.py", "PUT"),
                                                     ("nanosoc_rm/publish/client.py", "POST")}
    stray = dict(pub[0], file="nanosoc_rm/web/app.py")
    assert any("outside nanosoc_rm/publish/" in p for p in sw.allowlist_problems(WRITE_CALLS + [stray]))
    unguarded = dict(pub[0], guard="")
    assert sw.allowlist_problems(WRITE_CALLS[:-3] + [unguarded]) != []
    assert any("declares no guard" in p for p in sw.check_a6([], WRITE_CALLS + [unguarded]))
    census = sw.write_call_census(HERE.parents[2] / "src" / "nanosoc_rm")
    mine = [c for c in census if c["file"].startswith("nanosoc_rm/publish/")]
    assert {(c["file"], c["method"]) for c in mine} == {("nanosoc_rm/publish/client.py", "PUT"),
                                                       ("nanosoc_rm/publish/client.py", "POST"),
                                                       ("nanosoc_rm/publish/signing.py", "?")}
