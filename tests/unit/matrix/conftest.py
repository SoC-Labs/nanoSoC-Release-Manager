"""Fixtures for the matrix tests (D3, D4, D7, D8, D9, B7): a hermetic scene with real git hosts.

    github SoC-Labs/top@main pins `a` -> alpha@a1 and `b` -> beta@b1, and carries a component manifest whose
    requires[] gives alpha (path a) the range ~1.0 and names no range for beta.
    alpha: a1 v1.0.0 (pinned), a2 v1.0.1 (in range), a3 v1.1.0 (latest tag), a4 main head with v2.0.0-rc1 (a
    pre-release: never "latest"). beta: b1, its main head, no tags (latest-tag: none; head: same as pinned).

The walker's mirrors are synced from the hosts; the gates reach the same bare repos through compat's test-only
url_map seam, so a cell runs exactly the git commands of a real local T0 run.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "analysis"))
from analysis_fixtures import load_at  # noqa: E402

GH = "github"


class Scene:
    def __init__(self, at, h):
        self.h = h
        a = []
        for i, tag in enumerate(("v1.0.0", "v1.0.1", "v1.1.0", None)):
            a.append(h.commit(GH, "SoC-Labs/alpha", parents=a[-1:], files={"rtl/a.v": "module a%d; endmodule\n" % i}))
        h.push(GH, "SoC-Labs/alpha", a[-1], "main")
        for sha, tag in zip(a, ("v1.0.0", "v1.0.1", "v1.1.0", "v2.0.0-rc1")):
            h.tag(GH, "SoC-Labs/alpha", tag, sha)
        self.a = a
        self.b1 = h.commit(GH, "SoC-Labs/beta", files={"rtl/b.v": "module b; endmodule\n"})
        h.push(GH, "SoC-Labs/beta", self.b1, "main")
        manifest = {"schema": 1, "id": "top", "class": "soc_ref", "version": "1.0.0",
                    "canonical": "https://github.com/SoC-Labs/top.git", "provides": [],
                    "requires": [{"id": "alpha", "path": "a", "range": "~1.0"}], "reexports": [],
                    "variants": {"sim": {"flist": "flist/sim.f"}}, "tests": {"t0": {"run": "true", "control": "false"}}}
        self.top = h.commit(GH, "SoC-Labs/top", files={"README": "top\n",
                                                        "nanosoc-component.yaml": yaml.safe_dump(manifest)},
                            gitlinks={"a": a[0], "b": self.b1},
                            gitmodules=[{"path": "a", "url": h.url(GH, "SoC-Labs/alpha")},
                                        {"path": "b", "url": h.url(GH, "SoC-Labs/beta")}])
        h.push(GH, "SoC-Labs/top", self.top, "main")
        comps = ["top", "alpha", "beta"]
        catalogue = {"schema": 1, "components": [
            {"id": c, "class": "soc_ref" if c == "top" else "ip_tech",
             "canonical": {"host": GH, "path": "SoC-Labs/%s" % c}} for c in comps]}
        eligibility = {"schema": 1, "configurations": [
            {"id": "top-main", "component": "top", "ref": {"branch": "main"}}]}
        self.registry = at.write_registry(h.root / "registry", catalogue, eligibility, examples=False)
        self.env = h.tool_env(self.registry, "matrix")
        with self.env.active():
            for c in comps:
                at.rm("mirror.sync_component")(c)
            at.rm("walker.walk")("top-main")
        self.url_map = {"https://github.com/SoC-Labs/%s.git" % c: str(h.bare(GH, "SoC-Labs/%s" % c)) for c in comps}
        self.data = self.env.data_dir

    def run_cell(self, column, component=None, **kw):
        from nanosoc_rm.matrix import cells
        with self.env.active():
            return cells.run_cell("top-main", column, component, url_map=self.url_map, **kw)

    def clear_store(self):
        """Forget every stored matrix_cell and matrix_quarantine (the walk and mirrors stay)."""
        import sqlite3
        con = sqlite3.connect(str(self.data / "rm.sqlite"))
        con.execute("DELETE FROM observations WHERE kind IN ('matrix_cell', 'matrix_quarantine')")
        con.commit()
        con.close()
        jobs_db = self.data / "jobs.sqlite"
        if jobs_db.exists():
            jobs_db.unlink()


@pytest.fixture(scope="session")
def scene(tmp_path_factory):
    at = load_at("test_pins")
    h = at.LocalHosts(tmp_path_factory.mktemp("matrix"))
    yield Scene(at, h)
    h.api.close()


@pytest.fixture
def clean(scene):
    scene.clear_store()
    yield scene
    scene.clear_store()


@pytest.fixture(autouse=True)
def _developer_env(monkeypatch):
    for k in ("CI_JOB_ID", "CI_SERVER_URL", "RM_SCAN_RULES", "RM_SCAN_TOOLKIT", "RM_PROBE_URLS", "NANOSOC_RM_JOB"):
        monkeypatch.delenv(k, raising=False)


@pytest.fixture(autouse=True)
def _quiet_host(monkeypatch):
    """A quiet host for every attempt (the shared server's real load would make the D7 overload rule fire at
    random). A test that needs an overloaded host patches `provenance.host_load` itself."""
    from nanosoc_rm.matrix import provenance
    monkeypatch.setattr(provenance, "host_load", lambda: {"cpus": 16, "load": 1.0})
