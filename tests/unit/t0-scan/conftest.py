"""Fixtures for the t0 and scan unit tests: hermetic git hosts (local bare repos reached through the test-only
url_map seam), and the acceptance file's own fixture builders, loaded once so both use the same inputs."""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
EXAMPLES = REPO / "schemas" / "examples"


def _load_acceptance():
    name = "_rm_at_ci_errata"
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, REPO / "tests" / "acceptance" / "test_ci_errata.py")
        mod = importlib.util.module_from_spec(spec)
        sys.modules[name] = mod
        spec.loader.exec_module(mod)
    return sys.modules[name]


@pytest.fixture(scope="session")
def at():
    """tests/acceptance/test_ci_errata.py as a module: fx_commit, fx_publish, hermetic_gate_fixtures, the
    reference oracles and the example loaders."""
    return _load_acceptance()


@pytest.fixture(autouse=True)
def _no_ci_job(monkeypatch):
    """Unit tests run as a developer would, never as a CI job, and with no private rule file."""
    for k in ("CI_JOB_ID", "CI_SERVER_URL", "RM_SCAN_RULES", "RM_SCAN_TOOLKIT", "RM_PROBE_URLS", "NANOSOC_RM_CONFIG"):
        monkeypatch.delenv(k, raising=False)


WEB = "https://github.com/SoC-Labs/%s.git"
COPY = "https://gitlab.example.invalid/soclabs/%s.git"


class Tree:
    """A hermetic pinned tree on two fake hosts:

        rm-super (github)  pins  sub-a  @a1 by https            (canonical, main)
                                 sub-b  @b1 by ssh               (canonical, main; fetchable only by https)
                                     leaf @l2 by https            (reachable only from tag v1)
                                 sub-c  @c2 by the GitLab copy   (canonical github rm-sub-c lacks c2)
    """

    def __init__(self, at, root: Path):
        self.root = root
        bare = lambda n: root / ("%s.git" % n)
        self.url_map = {}
        f = at.fx_commit
        self.a1 = f(bare("rm-sub-a"), files={"rtl/a.v": "module a; endmodule\n"}, ref="refs/heads/main")
        self.l1 = f(bare("rm-leaf"), files={"rtl/l.v": "module l; endmodule\n"}, ref="refs/heads/main")
        self.l2 = f(bare("rm-leaf"), files={"rtl/l.v": "module l(input x); endmodule\n"}, parents=[self.l1],
                    ref="refs/tags/v1")
        self.b1 = f(bare("rm-sub-b"), files={"rtl/b.v": "module b; endmodule\n"}, gitlinks={"leaf": self.l2},
                    gitmodules={"leaf": WEB % "rm-leaf"}, ref="refs/heads/main")
        self.c1 = f(bare("rm-sub-c-copy"), files={"c.v": "module c; endmodule\n"})
        self.c2 = f(bare("rm-sub-c-copy"), files={"c.v": "module c(input y); endmodule\n"}, parents=[self.c1],
                    ref="refs/heads/main")
        at.fx_publish(bare("rm-sub-c-copy"), bare("rm-sub-c"), self.c1)     # canonical: only c1
        self.super = f(bare("rm-super"), files={"README": "super\n"},
                       gitlinks={"sub-a": self.a1, "sub-b": self.b1, "sub-c": self.c2},
                       gitmodules={"sub-a": WEB % "rm-sub-a", "sub-b": "git@github.com:SoC-Labs/rm-sub-b.git",
                                   "sub-c": COPY % "rm-sub-c"}, ref="refs/heads/main")
        for n in ("rm-sub-a", "rm-leaf", "rm-sub-b", "rm-sub-c", "rm-super"):
            self.url_map[WEB % n] = str(bare(n))
        self.url_map[COPY % "rm-sub-c"] = str(bare("rm-sub-c-copy"))
        self.request = {"configuration": "rm-fx", "repo": {"host": "github", "path": "SoC-Labs/rm-super"},
                        "commit": self.super, "upgrade_from": None,
                        "canonical_urls": {COPY % "rm-sub-c": WEB % "rm-sub-c"}}


@pytest.fixture(scope="session")
def tree(at, tmp_path_factory):
    return Tree(at, tmp_path_factory.mktemp("t0tree"))


@pytest.fixture
def runner_module():
    """nanosoc_rm.t0.runner, imported for one test and then forgotten: other tests (AT-A4, the CLI's own) prove
    the service process never imports it, so a unit test must not leave it in sys.modules."""
    import nanosoc_rm.t0 as pkg
    had = "nanosoc_rm.t0.runner" in sys.modules
    mod = importlib.import_module("nanosoc_rm.t0.runner")
    yield mod
    if not had:
        sys.modules.pop("nanosoc_rm.t0.runner", None)
        if hasattr(pkg, "runner"):
            delattr(pkg, "runner")


def rm_request(nonce: str, request: dict) -> str:
    return json.dumps(dict({"schema": 1, "nonce": nonce}, **request), separators=(",", ":"))
