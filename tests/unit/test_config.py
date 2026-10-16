"""nanosoc_rm.config: interfaces.md sections 1 and 17.1."""
from __future__ import annotations

import pytest

from nanosoc_rm.config import Config, ConfigError, load_config

# The shapes the three acceptance files write (tests/acceptance: scope_web write_config, ci_errata
# write_config, pins service_config). Each must load: a module asks for the keys it needs, the loader does not.
SCOPE_WEB = """
[service]
data_dir = "data"
[hosts.github]
web = "http://127.0.0.1:1"
api = "http://127.0.0.1:1"
ssh = "git@github.invalid:"
git = "file:///x/github"
org = "SoC-Labs"
[hosts.gitlab]
web = "http://127.0.0.1:2"
api = "http://127.0.0.1:2/api/v4"
ssh = "git@gitlab.invalid:"
group = "soclabs"
[registry]
repo = "soclabs/nanosoc-registry"
ref = "main"
path = "registry"
[ci]
project = "soclabs/nanosoc-compat-ci"
ref = "main"
job = "t0"
allowed_refs = ["main"]
fetch_timeout_minutes = 120
[credentials.github_read]
token_file = "tokens/github_read"
scope = ["contents:read", "metadata:read", "members:read"]
expires = "2027-06-30"
[credentials.github_oauth]
client_id = "fake-client-id"
token_file = "tokens/github_oauth"
scope = ["read:org"]
expires = ""
[session]
secret_file = "tokens/session"
[stores.disk]
path = "data/store"
[roles]
default = ["member"]
[schedule]
poll_minutes = 15
nightly_utc = "02:00"
fetch_minutes = 2
"""
CI_ERRATA = """
[service]
data_dir = "/abs/data"
[hosts.github]
web = "https://github.com"
api = "https://api.github.com"
[ci]
project = "soclabs/nanosoc-compat-ci"
project_id = 4242
allowed_runner_ids = [9001]
service_account = "svc-nanosoc-rm"
runner_tag = "rm-hardened"
runner_expected = {run_untagged = false, locked = true, access_level = "ref_protected", is_shared = false}
[paths]
quarantine = "/abs/data/quarantine"
[roles]
"member-a" = ["member"]
"""


def write(tmp_path, text):
    p = tmp_path / "rm.toml"
    p.write_text(text)
    return p


def test_minimal_config_loads_with_defaults(tmp_path):
    cfg = load_config(write(tmp_path, '[service]\ndata_dir = "data"\n'))
    assert isinstance(cfg, Config) and isinstance(cfg, dict)
    assert cfg.data_dir == tmp_path / "data"                      # relative to the config file
    assert cfg.db_path == tmp_path / "data" / "rm.sqlite"
    assert cfg.quarantine == tmp_path / "data" / "quarantine"
    assert cfg["schedule"] == {"poll_minutes": 15, "nightly_utc": "02:00", "fetch_minutes": 2}
    assert cfg["roles"] == {}                                     # an empty table refuses everyone


def test_acceptance_config_shapes_load(tmp_path):
    cfg = load_config(write(tmp_path, SCOPE_WEB))
    assert cfg.host("github")["git"] == "file:///x/github"
    assert cfg.host("gitlab")["git"] == "http://127.0.0.1:2"      # `git` defaults to `web`
    assert cfg["registry"]["path"] == str(tmp_path / "registry")
    assert cfg["credentials"]["github_read"]["token_file"] == str(tmp_path / "tokens" / "github_read")
    cfg = load_config(write(tmp_path, CI_ERRATA))
    assert cfg.require("ci", "project_id") == 4242
    assert cfg["roles"] == {"member-a": ["member"]}


def test_config_is_read_per_call(tmp_path, monkeypatch):
    p = write(tmp_path, '[service]\ndata_dir = "one"\n')
    monkeypatch.setenv("NANOSOC_RM_CONFIG", str(p))
    assert load_config().data_dir.name == "one"
    p.write_text('[service]\ndata_dir = "two"\n')
    assert load_config().data_dir.name == "two"
    other = tmp_path / "other.toml"
    other.write_text('[service]\ndata_dir = "three"\n')
    monkeypatch.setenv("NANOSOC_RM_CONFIG", str(other))
    assert load_config().data_dir.name == "three"


@pytest.mark.parametrize("text, needle", [
    ("", "[service] data_dir is required"),
    ("[service]\ndata_dir = 3\n", "data_dir must be str"),
    ("[service]\ndata_dir = 'd'\n[sevrice]\nx = 1\n", "unknown section [sevrice]"),
    ("[service]\ndata_dir = 'd'\n[ci]\nproject_id = '42'\n", "project_id must be int"),
    ("[service]\ndata_dir = 'd'\n[ci]\nallowed_runner_ids = [true]\n", "allowed_runner_ids must be list[int]"),
    ("[service]\ndata_dir = 'd'\n[ci]\nprojet = 'x'\n", "[ci] unknown key 'projet'"),
    ("[service]\ndata_dir = 'd'\n[hosts.bitbucket]\nweb = 'x'\n", "[hosts.bitbucket] unknown host key"),
    ("[service]\ndata_dir = 'd'\n[credentials.gitlab_read]\nexpires = '30/06/2027'\n", "expires must be YYYY-MM-DD"),
    ("[service]\ndata_dir = 'd'\n[credentials.gitlab_read]\nscope = 'read_api'\n", "scope must be list[str]"),
    ("[service]\ndata_dir = 'd'\n[schedule]\nnightly_utc = '2am'\n", "nightly_utc must be HH:MM"),
    ("[service]\ndata_dir = 'd'\n[roles]\ndefault = 'member'\n", "default must be a list of role names"),
    ("[service]\ndata_dir = 'd'\n[stores.s3]\nurl = 'x'\n", "[stores.s3] unknown store"),
    ("[service\n", "not valid TOML"),
])
def test_invalid_config_is_refused(tmp_path, text, needle):
    with pytest.raises(ConfigError) as e:
        load_config(write(tmp_path, text))
    assert needle in str(e.value)


def test_extra_credential_is_loaded_not_hidden(tmp_path):
    # A Phase 4 credential must reach `credentials check` and AT-A2 so they can report it.
    cfg = load_config(write(tmp_path, '[service]\ndata_dir = "d"\n[credentials.bump_writer]\nscope = ["api"]\n'))
    assert "bump_writer" in cfg["credentials"]


def test_missing_config(tmp_path, monkeypatch):
    monkeypatch.delenv("NANOSOC_RM_CONFIG", raising=False)
    with pytest.raises(ConfigError, match="NANOSOC_RM_CONFIG"):
        load_config()
    with pytest.raises(ConfigError, match="cannot read"):
        load_config(tmp_path / "absent.toml")


def test_require_names_the_missing_key(tmp_path):
    cfg = load_config(write(tmp_path, '[service]\ndata_dir = "d"\n[ci]\nproject = "p"\n'))
    with pytest.raises(ConfigError, match=r"\[ci\] project_id is required"):
        cfg.require("ci", "project_id")
    with pytest.raises(ConfigError, match=r"\[credentials.ci_trigger\] token_file"):
        cfg.require("credentials", "ci_trigger", "token_file")
    with pytest.raises(ConfigError, match=r"\[hosts.gitlab\]"):
        cfg.host("gitlab")


def test_read_secret_never_leaks(tmp_path):
    (tmp_path / "tok").write_text("s3cr3t-token\n")
    (tmp_path / "empty").write_text("\n")
    cfg = load_config(write(tmp_path, '[service]\ndata_dir = "d"\n'
                                      '[credentials.gitlab_read]\ntoken_file = "tok"\n'
                                      '[credentials.github_read]\ntoken_file = "empty"\n'
                                      '[credentials.ci_trigger]\ntoken_file = "absent"\n'))
    assert cfg.read_secret("gitlab_read") == "s3cr3t-token"
    for name in ("github_read", "ci_trigger"):
        with pytest.raises(ConfigError) as e:
            cfg.read_secret(name)
        assert "s3cr3t" not in str(e.value) and name in str(e.value)


def test_api_readers_are_checked_and_never_share_a_token_with_an_agent(tmp_path):
    """Section 26.7 (integration review 2026-10-16): [[api.readers]] holds bump-check reader tokens (digests only);
    one digest in both lists would let one token open two doors, so the config is refused."""
    from nanosoc_rm.config import ConfigError, load_config
    from nanosoc_rm.executor import tokens
    d = tokens.digest(tokens.new_token())
    base = '[service]\ndata_dir = "%s"\n' % (tmp_path / "data")
    ok = tmp_path / "ok.toml"
    ok.write_text(base + '[[api.readers]]\nname = "eth-ci"\ntoken_sha256 = "%s"\n' % d)
    assert load_config(ok)["api"]["readers"][0]["name"] == "eth-ci"
    for bad, words in (('[[api.readers]]\nname = "eth-ci"\ntoken_sha256 = "abc"\n', "token_sha256 is required"),
                       ('[api]\nwriters = []\n', "unknown key"),
                       ('[[api.readers]]\nname = "a"\ntoken_sha256 = "%s"\n[[executor.agents]]\nname = "eda-1"\n'
                        'token_sha256 = "%s"\nlabels = ["jobs:test-bump"]\n' % (d, d), "repeats")):
        p = tmp_path / "bad.toml"
        p.write_text(base + bad)
        with pytest.raises(ConfigError, match=words):
            load_config(p)


def test_token_new_reader_prints_an_api_readers_entry(tmp_path):
    from click.testing import CliRunner

    from nanosoc_rm.cli import main
    out = tmp_path / "r.token"
    r = CliRunner().invoke(main, ["executor", "token", "new", "eth-ci", "--out", str(out), "--reader"])
    assert r.exit_code == 0, r.output
    assert "[[api.readers]]" in r.output and "[[executor.agents]]" not in r.output
    assert out.read_text().strip() not in r.output                       # the token itself is never printed
    r = CliRunner().invoke(main, ["executor", "token", "new", "x", "--out", str(tmp_path / "x"), "--reader",
                                  "--labels", "vcs"])
    assert r.exit_code != 0
