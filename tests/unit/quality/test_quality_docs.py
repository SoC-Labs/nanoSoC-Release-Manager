"""Q4: conciseness scores rank a padded doc below its concise twin; the optional rewrite makes no network call
without a key, and with one it keeps every command and path verbatim or the rewrite is dropped."""
from __future__ import annotations

import json
import socket

import httpx
import pytest
from click.testing import CliRunner

from nanosoc_rm import contracts, quality
from nanosoc_rm.cli import main
from nanosoc_rm.quality import rewrite
from nanosoc_rm.quality.prose import score_text
from nanosoc_rm.quality.source import WorktreeSource
from qfix import git, make_repo

pytestmark = pytest.mark.requirement("Q4")

CONCISE = """# Build

Run `make bitstream` in `fpga/`. The bitstream lands in `build/top.bit`.

Flash it with `scripts/flash.sh`. The script checks the board ID first.
"""
PADDED = """# Build

In order to build the bitstream, it should be noted that you basically just need to simply run `make bitstream`,
which is generally run in the `fpga/` directory, and it is important to note that the bitstream that is produced by
this step is actually written to `build/top.bit` by the tools, which might perhaps take quite a while.

Please note that the bitstream is flashed by the `scripts/flash.sh` script, and it is worth noting that the board ID
is checked by the script first, and of course it is really important to make sure to check it.
"""


def test_padded_scores_below_its_concise_twin():
    c, p = score_text(CONCISE), score_text(PADDED)
    assert c["score"] > 90 and p["score"] < 50, (c, p)
    assert p["filler"] >= 10 and p["passive"] >= 2 and p["mean_sentence"] > 20
    assert c["filler"] == 0 and c["passive"] == 0


def test_ranking_and_cross_file_duplicates(tmp_path):
    para = ("The die-to-die link trains on reset and holds the lanes in lock until the peer drops its ready signal, "
            "after which both dies return to the idle state and wait for a new handshake from the host.")
    repo = make_repo(tmp_path / "r", {"docs/concise.md": CONCISE, "docs/padded.md": PADDED,
                                      "a/README.md": "# A\n\n" + para + "\n", "b/README.md": "# B\n\n" + para + "\n"})
    obs = quality.docs(None, path=repo)
    assert contracts.validate_observation(obs) == []
    f = obs["fact"]
    order = [x["path"] for x in f["files"]]
    assert order.index("docs/padded.md") < order.index("docs/concise.md"), "least concise first"
    assert [(d["a"], d["b"]) for d in f["duplicates"]] == [("a/README.md:3", "b/README.md:3")]
    rows = {x["path"]: x for x in f["files"]}
    assert rows["a/README.md"]["duplicated"] == 1 and rows["docs/concise.md"]["duplicated"] == 0


@pytest.fixture
def no_network(monkeypatch):
    """Any socket connect or httpx client construction fails the test."""
    def refuse(*a, **k):
        raise AssertionError("network call attempted without an API key")
    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(httpx.Client, "__init__", refuse)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("NANOSOC_RM_CONFIG", raising=False)


def test_no_key_means_no_network_call(tmp_path, no_network):
    repo = make_repo(tmp_path / "r", {"README.md": PADDED})
    src = WorktreeSource(repo)
    res = rewrite.rewrite_sections(src, quality.docs(None, path=repo)["fact"])
    assert res["enabled"] is False and res["proposals"] == [] and res["patch"] == ""
    out = CliRunner().invoke(main, ["quality", "docs", "--path", str(repo), "--rewrite"])
    assert "no network call was made" in out.output and out.exit_code in (0, 1)


def _transport(reply_for):
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen.append((request, body))
        text = reply_for(body["messages"][0]["content"])
        return httpx.Response(200, json={"stop_reason": "end_turn", "content": [{"type": "text", "text": text}]})
    return httpx.MockTransport(handler), seen


def test_rewrite_with_key_proposes_a_patch_that_applies(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-not-real")
    monkeypatch.delenv("NANOSOC_RM_CONFIG", raising=False)
    repo = make_repo(tmp_path / "r", {"README.md": PADDED})
    transport, seen = _transport(lambda section: CONCISE.rstrip("\n"))
    client = httpx.Client(base_url=rewrite.API_BASE, transport=transport)
    res = rewrite.rewrite_sections(WorktreeSource(repo), quality.docs(None, path=repo)["fact"], client=client)
    assert res["enabled"] and len(res["proposals"]) == 1 and res["rejected"] == []
    req, body = seen[0]
    assert req.url.path == "/v1/messages" and req.headers["x-api-key"] == "test-key-not-real"
    assert body["model"] == "claude-sonnet-5-5" and "exactly as written" in body["system"]
    out = tmp_path / "rw.diff"
    out.write_text(res["patch"])
    git("apply", "--check", str(out), cwd=repo)


def test_a_rewrite_that_drops_a_command_is_rejected(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-not-real")
    monkeypatch.delenv("NANOSOC_RM_CONFIG", raising=False)
    repo = make_repo(tmp_path / "r", {"README.md": PADDED})
    transport, _ = _transport(lambda section: "# Build\n\nRun make. Flash the board.")
    client = httpx.Client(base_url=rewrite.API_BASE, transport=transport)
    res = rewrite.rewrite_sections(WorktreeSource(repo), quality.docs(None, path=repo)["fact"], client=client)
    assert res["proposals"] == [] and res["patch"] == ""
    assert "verbatim" in res["rejected"][0]["reason"]
