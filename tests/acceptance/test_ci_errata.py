"""Gate-2 acceptance tests: CI, T0 and errata.

One acceptance test per v1 Must requirement D1, D2, D5, D6, D10, D11, F1, F2, F3 and F5,
each with its negative control (PR6). The Given/When/Then, fixtures, and the reason each test
cannot pass vacuously are in docs/acceptance/ci_errata.md. The AT id in each docstring points
there.

Every acceptance test carries NOT_BUILT (xfail, raises=ImportError, strict): XFAIL until the module
it exercises exists, red on any other error, red as XPASS(strict) once it passes, so its builder
removes the marker (tests/conftest.py, interfaces.md section 17.4). The assertions are written against
the gate-2 contracts (docs/contracts/, schemas/). A name marked PROPOSED (Gn) was a gap in the first
draft; interfaces.md section 17.7 says where each one is now contracted.

test_selfcheck_* are not acceptance tests of the tool. They run now and prove each negative control's
input is what the AT claims, with an oracle that shares no tool code (PR6 at checker level).

Markers (registered in tests/conftest.py):
    requirement("<ID>")  the requirement this test accepts
    live                 needs the lab network, the service's data or the registry checkout;
                         the integrator's end-to-end run (PR5)

Lab inputs for live tests (environment variables, gap G13):
    NANOSOC_RM_CONFIG      the private service config (interfaces.md section 1)
    NANOSOC_RM_REGISTRY    a checkout of the registry's main branch
    RM_ARM_REFERENCE_FILE  one real vendor source file, read in place, read-only (AT-D11 realism)
    RM_RC4_BUILD_SOC_TAR   the P1 archive build_soc.tar.gz of rc4 (AT-F2)

Records are handled as schema documents (dicts). as_doc() converts a model or dataclass (G14).
"""
from __future__ import annotations

import copy
import dataclasses
import datetime as dt
import importlib.util
import json
import os
import shutil
import subprocess
from html import unescape as html_unescape
from html.parser import HTMLParser
from pathlib import Path

import pytest

GATE2 = "not implemented: gate 2"
NOT_BUILT = pytest.mark.xfail(raises=ImportError, strict=True, reason=GATE2)

ROOT = Path(__file__).resolve().parents[2]
SCHEMAS = ROOT / "schemas"
EX = SCHEMAS / "examples"
BAD = EX / "invalid"

# --------------------------------------------------------------------------------------------
# Real facts. OBSERVED 2026-09-30 with read-only git and API calls (doc: "Real facts").
# --------------------------------------------------------------------------------------------
RC4 = "ab73e40924317e45e7b3b941f52a7ed35f20832e"            # tags v1.0.0, silicon/eth-chiplet-r0p0
RC4_PARENT = "3ab16f500dc43cd9ee436ef3a5dc753c1d5a70cd"     # ab73e40's first parent (GitHub API)
RC4_PIN_COUNT = 44
RC4_SSH_PINS = ("tidelink", "ASIC/asic-toolkit")            # the only ssh URLs in rc4's tree
RC4_BELOW_TIDELINK = 5                                      # pins below tidelink, never reached anonymously
TIDELINK_PIN = "5e8bdb5a2141ff5a82103813a326a2c9df2b43ed"   # = v0.90 on GitHub; MISSING on the GitLab copy
AHB_QSPI_GITLAB_ONLY = "e908ac843845d5b293616265f93a802804555540"  # rc4's ahb_qspi pin; on GitHub since the
                                                                   # 2026-10-01 tag keep/eth-rc4/ahb_qspi
# CR-2: a commit that IS GitLab-only today (anonymous fetch 2026-10-10: GitLab ok, GitHub "not our ref").
AHB_QSPI_GITLAB_ONLY_NOW = "1e5117e1e8aec174c4bcd9bf17110cd732b57382"
AHB_QSPI_GITHUB_ONLY = "71f8d1ce46dfa76e9f1e16f7ab186e51d1b31520"  # GitHub master; GitLab answers 404
ASIC_FLOW_GITHUB_ONLY = "3824aaaf9cee33b647d149b5f00b55d44022b488"  # rc4's ASIC/asic-flows pin; GitHub
                                                                   # lpddr4-pll (its default) only
# rc4's other two asic-flow pins, 46c88d3 and 69500ef, are on main on both hosts.
# rc4's arch_tech pin ecf76aa: keep tag on the GitLab copy only; on GitHub (canonical) it is reachable
# only from branch wip/uncommitted-2026-08-18. One branch deletion from failing t0.pins.reachable.
ARCH_TECH_GITHUB_BRANCH_ONLY = "ecf76aa569c4cd4fd05f9220a87dc428ee7c09c3"
# The 11 rc4 pins that were branch-only and are now PROTECTED_BY_TAG by keep/eth-rc4/* (pinned host).
RC4_KEEP_TAG_PINS = (
    "tidechart", "tidelink/deps/axi-chiplet-controller", "tidelink/deps/tidelink-gpio-phy",
    "tidelink/deps/tidelink-phy", "nanosoc-multicore-system",
    "nanosoc-multicore-system/ethernet-subsystem-ahb",
    "nanosoc-multicore-system/ethernet-subsystem-ahb/ethernet-mac-ahb",
    "nanosoc-multicore-system/nanosoc_arch_tech",
    "nanosoc-multicore-system/nanosoc_arch_tech/nanosoc_gen",
    "nanosoc-multicore-system/nanosoc_arch_tech/rtl/hostio4", "ASIC/asic-toolkit",
)
# Third-party repos pinned in rc4's tree (catalogue `external`, all on GitHub): their own repo is canonical.
RC4_EXTERNALS = ("core_dbg_bridge", "picotcp", "libtelnet")
GEN_PINS_RC4 =("d04de2ec25d79c1fc3223f853ddb6725cbf2831c",  # builds rc4's RTL
                "1d7772dd0cd0e135f86f7d9906f4852883c4670e")  # nested under ethernet-subsystem-ahb
ORIGIN_ARCH = "49a6c252c34cbc6bcd5b715b5f37bae176417a12"    # nanosoc_arch_tech, on GitHub main
ORIGIN_GEN = "b0a606960c349aeac4a00f67b3dbd13f55cb5f24"     # nanosoc_gen, on main
FIX_FORM_A = "036d3811e630c89b2344b56aa617b40a99cd652b"     # nanosoc_gen main
FIX_FORM_B = "adbb9abfaa81350e6f40d3b5c4e20f1f87b8002d"     # nanosoc_gen main
BENCH_UNPUSHED = "6aafc943f2750be92e80b31be99f0cd60f8927e4"  # eth; GitHub answers 422
WORKAROUND_BRANCH_ONLY = "7a87e20051d6335abd3de690cde332bf3bd935a9"  # multicore feat/soc-completeness-gate
IMPORT_DOCS_COMMIT = "66a2f698d6650e6e4e5beba1434b22231846696a"      # eth; not on main; ancestor of rc4
QS_WRITEUP_LOCAL_ONLY = "1f2f06e4303de5eaa7316caf965ec0b070a89028"  # QuickStart; GitHub answers 422
RC4_BUILD_SOC_MD5 = "80eb7ce7e81fee653b52c6821cea3942"     # P1 archive, 332 files
MULTICORE_DMA250 = "57d2c762aab6f6b403e9747c7a390e11c3c4dcac"  # PL230 -> DMA-250; ancestor of f60449b and master
MULTICORE_RC4_PIN = "f60449b491d54b617c92f1ccf0538c187cd8f34a"  # rc4's multicore pin; keep/eth-rc4 tag

ETH_REPO = ("github", "SoC-Labs/NanoSoC-Ethernet-Chiplet")
COMPUTE_REPO = ("github", "SoC-Labs/NanoSoC-Compute-Chiplet")
QS_REPO = ("github", "SoC-Labs/nanoSoC-M0-QuickStart-SoC")
ETH_HTTPS = "https://github.com/SoC-Labs/NanoSoC-Ethernet-Chiplet.git"

# rc4's .gitmodules at ab73e40, verbatim except the lab GitLab host.
RC4_GITMODULES = """\
[submodule "tidechart"]
\tpath = tidechart
\turl = https://<gitlab-host>/soclabs/tidechart.git
[submodule "tidelink"]
\tpath = tidelink
\turl = git@github.com:SoC-Labs/TideLink-Chiplet-Interconnect-AHB.git
[submodule "nanosoc-multicore-system"]
\tpath = nanosoc-multicore-system
\turl = https://<gitlab-host>/soclabs/nanosoc-multicore-system.git
[submodule "ASIC/asic-flows"]
\tpath = ASIC/asic-flows
\turl = https://github.com/SoC-Labs/ASIC-Flow.git
\tbranch = lpddr4-pll
[submodule "ASIC/asic-toolkit"]
\tpath = ASIC/asic-toolkit
\turl = git@github.com:SoC-Labs/nanoSoC-ASIC-Toolkit.git
\tbranch = main
"""

KNOWN_ISSUES_KEYS = tuple("%s)" % c for c in "abcdefghi")   # 11-known-issues.md @ 66a2f698
OPEN_DEFECTS_NUMBERED = ("1", "2", "3", "4", "5", "6")       # 16-open-defects.md @ 66a2f698
OPEN_DEFECTS_CANDIDATES = 9   # the 6 numbered + 2 "found in passing" + 1 in the 2026-08-12 addendum

# Gates contracted in interfaces.md section 9.
CONFIG_GATES = (
    "t0.infra.probe", "t0.runner.hardened", "t0.pins.fetchable", "t0.pins.reachable",
    "t0.urls.no-ssh-public", "t0.manifest.valid", "t0.clone.upgrade-path", "t0.vendor.no-arm-ip",
)
REGISTRY_GATES = ("t0.registry.schema", "t0.registry.refs-resolve")

# Control state for a NOT-RUN gate (observation schema: control `not-applicable` iff verdict NOT-RUN).
NOT_RUN_CONTROL = "not-applicable"

# The golden rc4 roll-up (README section 6 applied to result-bundle.verdicts.example.tsv, which now
# carries the two t0.runner.hardened rows; G6).
EXPECTED_RC4_ROLLUP = {
    "t0.infra.probe": ("PASS", "fired"),
    "t0.runner.hardened": ("PASS", "fired"),
    "t0.pins.fetchable": ("FAIL", "fired"),
    "t0.pins.reachable": ("UNVERIFIED", "fired"),   # CR-2: e908ac8 on GitHub since 10-01; the toolkit is private
    "t0.urls.no-ssh-public": ("FAIL", "fired"),
    "t0.manifest.valid": ("NOT-RUN", NOT_RUN_CONTROL),
    "t0.clone.upgrade-path": ("NOT-RUN", NOT_RUN_CONTROL),
    "t0.vendor.no-arm-ip": ("UNVERIFIED", "fired"),
}

# GATE2 Q1 (decided 2026-10-01): with the 7 t0_waivers of eligibility.example.yaml (8 until CR-2), rc4's four non-pass
# gates roll up WAIVED (README section 6). The raw roll-up above is unchanged; each WAIVED gate stores it
# as raw_verdict.
RC4_WAIVED_GATES = ("t0.pins.fetchable", "t0.pins.reachable", "t0.urls.no-ssh-public", "t0.vendor.no-arm-ip")
EXPECTED_RC4_WAIVED_ROLLUP = dict(EXPECTED_RC4_ROLLUP, **{g: ("WAIVED", "fired") for g in RC4_WAIVED_GATES})
AHB_QSPI_TREE_PATH = "nanosoc-multicore-system/ahb_qspi"
# CR-2: the waiver eligibility.example.yaml carried until e908ac8 reached GitHub (tag keep/eth-rc4/ahb_qspi,
# 2026-10-01), and the item it covered. The waiver-retirement checks below replay that history.
RETIRED_AHB_QSPI_WAIVER = {"gate": "t0.pins.reachable", "tree_path": AHB_QSPI_TREE_PATH, "result": "fail",
                           "approved_by": "member-a", "approved_on": "2026-10-01",
                           "reason": "e908ac8 exists only on the GitLab copy (retired by CR-2)"}
AHB_QSPI_UNREACHABLE_ITEM = {"tree_path": AHB_QSPI_TREE_PATH, "component": "ahb_qspi",
                             "commit": "e908ac843845d5b293616265f93a802804555540", "host": "github", "result": "fail",
                             "why": "e908ac8 is on no branch or tag of the canonical repo SoC-Labs/AHB-QSPI (before 2026-10-01)"}
# SYNTHETIC: a failure rc4's waivers do not cover (a pin that vanished from its canonical repo).
NEW_FAILURE = {"tree_path": "tidechart", "component": "tidechart", "commit": "1" * 40, "host": "gitlab",
               "result": "fail", "why": "SYNTHETIC: the pinned commit is on no branch or tag any more"}

# F1 fields of an erratum (erratum.schema.json).
ERRATUM_F1_FIELDS = ("forms_present", "effects_present", "exposure", "category", "conditions",
                     "workaround", "status", "doc_revision")

# Expected hardened-runner settings (proposal section 4.3; values checked by G7).
EXPECTED_RUNNER_SETTINGS = {"run_untagged": False, "access_level": "ref_protected",
                            "locked": True, "is_shared": False}

# SYNTHETIC. The CI project, pipeline, job and runner do not exist yet.
CI_PROJECT = "soclabs/nanosoc-compat-ci"
CI_PROJECT_ID = 4242
OTHER_PROJECT_ID = 4343
RUNNER_ID = 9001
SERVICE_ACCOUNT = "svc-nanosoc-rm"
MEMBER = "member-a"
OTHER_RUN_NONCE = "5f0c3b1e9a7d4c2b8e6f1a0d3c5b7e9f"   # the rejected run in observation.example.jsonl

# A runner log as the T0 job writes it. Two lines, so a planted specimen starts at line 3.
T0_LOG = ("t0: request eth-rc4 ab73e40924317e45e7b3b941f52a7ed35f20832e\n"
          "t0: fetch https://github.com/SoC-Labs/NanoSoC-Ethernet-Chiplet.git (anonymous, depth 1)\n")


# --------------------------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------------------------
def _check_examples():
    """Reuse the contract checker (PR9): its YAML loader keeps timestamps as strings and
    refuses duplicate keys, as the registry loader must."""
    spec = importlib.util.spec_from_file_location("check_examples", SCHEMAS / "check_examples.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def load_yaml(path):
    import yaml
    return yaml.load(Path(path).read_text(), Loader=_check_examples().StrictLoader)


def dump_yaml(doc, path):
    import yaml
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(doc, sort_keys=False))
    return path


def as_doc(obj):
    """A record as its schema document. G14: the record types are not contracted."""
    if isinstance(obj, dict):
        return obj
    if hasattr(obj, "model_dump"):
        return obj.model_dump(mode="json", exclude_none=False)
    if dataclasses.is_dataclass(obj):
        return dataclasses.asdict(obj)
    raise TypeError("cannot read %r as a schema document" % (obj,))


def utc(text):
    return dt.datetime.strptime(text.rstrip("Z").split(".")[0], "%Y-%m-%dT%H:%M:%S").replace(
        tzinfo=dt.timezone.utc)


def now_utc():
    return dt.datetime.now(dt.timezone.utc)


def require_env(name):
    """A live test without its lab input FAILS; it never passes by checking nothing."""
    value = os.environ.get(name)
    if not value:
        pytest.fail("%s is not set: this acceptance test needs the lab input it names" % name)
    return value


def private_config():
    import tomllib  # Python 3.11
    return tomllib.loads(Path(require_env("NANOSOC_RM_CONFIG")).read_text())


def rm(*args, timeout=900):
    """Run the CLI the way a person would (PR4)."""
    return subprocess.run(["nanosoc-rm", *[str(a) for a in args]], capture_output=True, text=True,
                          timeout=timeout)


def rm_json(*args):
    cp = rm(*args)
    assert cp.returncode != 3, "usage or config error: %s" % cp.stderr
    return cp.returncode, [json.loads(line) for line in cp.stdout.splitlines() if line.strip()]


def gate_items(nonce, gate):
    """Per-item results of one gate in one run. PROPOSED (G11): `nanosoc-rm t0 items`."""
    rc, items = rm_json("t0", "items", nonce, gate, "--json")
    assert rc in (0, 1), rc
    assert items, "gate %s of run %s reported no items: it checked nothing" % (gate, nonce)
    return items


def _git(args, cwd):
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0", GIT_CONFIG_GLOBAL="/dev/null",
               GIT_CONFIG_NOSYSTEM="1", GIT_SSH_COMMAND="ssh -o BatchMode=yes")
    return subprocess.run(["git", *args], cwd=str(cwd), env=env, capture_output=True, text=True,
                          timeout=300)


def anon_show(workdir, url, sha, path):
    """Independent oracle: the file at `sha`, fetched anonymously, or None if not fetchable."""
    workdir = Path(workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    _git(["init", "-q", "."], workdir)
    if _git(["fetch", "-q", "--depth=1", url, sha], workdir).returncode != 0:
        return None
    cp = _git(["show", "%s:%s" % (sha, path)], workdir)
    return cp.stdout if cp.returncode == 0 else None


def anon_fetchable(workdir, url, sha):
    workdir = Path(workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    _git(["init", "-q", "."], workdir)
    return _git(["fetch", "-q", "--depth=1", url, sha], workdir).returncode == 0


class _Attrs(HTMLParser):
    def __init__(self, key):
        super().__init__()
        self.key, self.found = key, []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if self.key in a:
            self.found.append(a)


def elements_with(html, key):
    """Start tags carrying data attribute `key`. Page markup is PROPOSED (G5)."""
    p = _Attrs(key)
    p.feed(html)
    return p.found


def example_provenance():
    return json.loads((EX / "result-bundle.provenance.example.json").read_text())


def example_verdicts():
    return (EX / "result-bundle.verdicts.example.tsv").read_text()


def example_items():
    """{gate: gate_item lines}: rc4's non-pass items (EXCERPTS: README section 7)."""
    pre, suf = "result-bundle.items.", ".example.jsonl"
    return {f.name[len(pre):-len(suf)]: [json.loads(line) for line in f.read_text().splitlines() if line]
            for f in sorted(EX.glob(pre + "*" + suf))}


def example_waivers(config="eth-rc4"):
    cfgs = {c["id"]: c for c in load_yaml(EX / "eligibility.example.yaml")["configurations"]}
    return cfgs[config].get("t0_waivers", [])


FIELDS = ("utc", "status", "gate", "detail", "nonce")


def parse_rows(tsv):
    return [dict(zip(FIELDS, line.split("\t"))) for line in tsv.splitlines() if line]


def format_rows(rows):
    return "".join("\t".join(r[k] for k in FIELDS) + "\n" for r in rows)


def with_runner_hardened(tsv, nonce):
    """The two t0.runner.hardened rows a real run writes. The golden example carries them since the
    gate-2 revision (G6), so this adds them only when absent; it never doubles a row."""
    rows = parse_rows(tsv)
    if any(r["gate"] == "t0.runner.hardened" for r in rows):
        return format_rows(rows)
    rows[2:2] = [
        dict(utc="2026-10-01T02:03:43Z", status="PASS", gate="t0.runner.hardened",
             detail="protected ref; HOME and TMPDIR empty at start; user is the service account",
             nonce=nonce),
        dict(utc="2026-10-01T02:03:43Z", status="PASS", gate="t0.runner.hardened.negctl",
             detail="synthetic environment with an unprotected ref failed, as it must", nonce=nonce),
    ]
    return format_rows(rows)


def request_bytes(prov):
    """RM_REQUEST exactly as the service stores and sends it (interfaces.md section 9)."""
    req = {"schema": 1, "nonce": prov["run_nonce"]}
    req.update(prov["request"])
    return json.dumps(req, separators=(",", ":")).encode()


def write_bundle(root, prov, verdicts, files=None):
    """rm-bundle/ as the job leaves it: provenance.json, verdicts.tsv, logs/."""
    b = Path(root) / "rm-bundle"
    (b / "logs").mkdir(parents=True, exist_ok=True)
    (b / "provenance.json").write_text(json.dumps(prov, indent=2))
    (b / "verdicts.tsv").write_text(verdicts)
    (b / "logs" / "t0.log").write_text(T0_LOG)
    for rel, text in (files or {}).items():
        p = b / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    return b


def write_config(tmp_path, monkeypatch):
    """A throwaway service config. Section names are contracted (interfaces.md section 1);
    key names are PROPOSED (G4, G7, G8). `gitlab.invalid` stands in for the lab host."""
    data = Path(tmp_path) / "data"
    (data / "quarantine").mkdir(parents=True, exist_ok=True)
    path = Path(tmp_path) / "rm.toml"
    path.write_text("\n".join([
        "[service]", 'data_dir = "%s"' % data,
        "[hosts.github]", 'web = "https://github.com"', 'api = "https://api.github.com"',
        "[hosts.gitlab]", 'web = "https://gitlab.invalid"', 'api = "https://gitlab.invalid/api/v4"',
        "[registry]", 'repo = "soclabs/nanosoc-registry"', 'ref = "main"',
        'path = "%s"' % (Path(tmp_path) / "registry"),
        "[ci]", 'project = "%s"' % CI_PROJECT, "project_id = %d" % CI_PROJECT_ID, 'ref = "main"',
        'job = "t0"', 'allowed_refs = ["main"]', "allowed_runner_ids = [%d]" % RUNNER_ID,
        'service_account = "%s"' % SERVICE_ACCOUNT, 'runner_tag = "rm-hardened"',
        "fetch_timeout_minutes = 120",
        "[paths]", 'quarantine = "%s"' % (data / "quarantine"),
        "[schedule]", "poll_minutes = 15", 'nightly_utc = "02:00"', "fetch_minutes = 2",
        "[roles]", '"%s" = ["member"]' % MEMBER,
        "",
    ]))
    monkeypatch.setenv("NANOSOC_RM_CONFIG", str(path))
    return path


def ci_api_records(prov, req):
    """The CI API's own record of the job, in GitLab's response shapes (GET pipeline, jobs,
    variables). The service trusts these, not the bundle (D5)."""
    ci = prov["ci"]
    pipeline = {"id": ci["pipeline_id"], "project_id": CI_PROJECT_ID, "ref": ci["ref"],
                "sha": ci["harness_sha"], "source": "trigger", "status": "success"}
    job = {"id": ci["job_id"], "name": ci["job_name"], "ref": ci["ref"], "status": "success",
           "started_at": "2026-10-01T02:03:28Z", "finished_at": "2026-10-01T02:05:04Z",
           "pipeline": {"id": ci["pipeline_id"], "project_id": CI_PROJECT_ID, "ref": ci["ref"],
                        "sha": ci["harness_sha"]},
           "runner": {"id": RUNNER_ID, "description": "rm-hardened", "is_shared": False,
                      "runner_type": "project_type"}}
    variables = [{"key": "RM_REQUEST", "value": req.decode(), "variable_type": "env_var"}]
    return pipeline, job, variables


@dataclasses.dataclass
class VerifyCase:
    run: dict            # RunRecord fields = the `runs` table columns (G4)
    prov: dict
    pipeline: dict
    job: dict
    variables: list
    verdicts: str


def genuine_case():
    """The genuine rc4 T0 result: example provenance and rows, and matching CI API records."""
    prov = example_provenance()
    req = request_bytes(prov)
    pipeline, job, variables = ci_api_records(prov, req)
    run = dict(nonce=prov["run_nonce"], configuration="eth-rc4", commit=RC4, request=req,
               trigger="schedule", triggered_by="schedule", triggered_at="2026-10-01T02:03:10Z",
               pipeline_id=prov["ci"]["pipeline_id"], job_id=None, state="pending")
    verdicts = with_runner_hardened(example_verdicts(), prov["run_nonce"])
    return VerifyCase(run, prov, pipeline, job, variables, verdicts)


def run_verify(case, root, files=None):
    from nanosoc_rm.ci.verify import RunRecord, verify   # verify: contracted; RunRecord: G4
    bundle = write_bundle(root, case.prov, case.verdicts, files)
    return verify(RunRecord(**case.run), case.pipeline, case.job, case.variables, bundle)


def fetch_via_fake_ci(case, root, files=None):
    """End to end through fetch_pending(): the stored pending run, a fake CI API serving the
    records and the zipped bundle, then the stored t0_result. PROPOSED seams (G4)."""
    from nanosoc_rm import db
    from nanosoc_rm.ci.client import FakeCI
    from nanosoc_rm.ci.fetch import fetch_pending
    from nanosoc_rm.ci.verify import RunRecord
    bundle = write_bundle(root, case.prov, case.verdicts, files)
    db.put_run(RunRecord(**case.run))
    fake = FakeCI(pipelines={case.pipeline["id"]: case.pipeline},
                  jobs={case.pipeline["id"]: [case.job]},
                  variables={case.pipeline["id"]: case.variables},
                  artifacts={case.job["id"]: bundle})
    out = [as_doc(o) for o in fetch_pending(client=fake)]
    mine = [o for o in out if o["fact"]["run"]["nonce"] == case.run["nonce"]]
    assert len(mine) == 1, out
    return mine[0]["fact"]


def example_registry(root):
    """A registry directory built from the contract examples (README section 1 layout)."""
    root = Path(root)
    for src, dst in (("component-catalogue.example.yaml", "catalogue.yaml"),
                     ("eligibility.example.yaml", "eligibility.yaml"),
                     ("defect.SL-DEF-0001.example.yaml", "defects/SL-DEF-0001.yaml"),
                     ("erratum.ETH-R0P0-ERR-001.example.yaml", "errata/eth-rc4/ERR-001.yaml"),
                     ("import-ledger.example.yaml", "imports.yaml")):
        (root / dst).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(EX / src, root / dst)
    return root


def prime_for_trigger(tmp_path):
    """CR-3: what section 10 Trigger step 1 reads, for the example registry under tmp_path/registry: the registry
    as a git checkout on `main` (the @registry job asks for its head), and each non-frozen configuration's ref
    and upgrade_from in its component's mirror (section 3 layout, canonical repo's namespace)."""
    from nanosoc_rm.mirror.layout import ref_prefix
    reg = Path(tmp_path) / "registry"
    _fx_git("init", "-q", "-b", "main", reg)
    _fx_git("-C", reg, "add", "-A")
    _fx_git("-C", reg, "commit", "-q", "-m", "registry")
    comps = {c["id"]: c for c in load_yaml(reg / "catalogue.yaml")["components"]}
    for c in load_yaml(reg / "eligibility.yaml")["configurations"]:
        if c.get("frozen"):
            continue
        canon = comps[c["component"]]["canonical"]
        mirror = Path(tmp_path) / "data" / "mirrors" / ("%s.git" % c["component"])
        for ref in filter(None, (c.get("ref"), c.get("upgrade_from"))):
            kind, name = ("heads", ref["branch"]) if "branch" in ref else ("tags", ref["tag"])
            fx_commit(mirror, files={"README": "%s %s\n" % (c["id"], name)},
                      ref="%s/%s/%s" % (ref_prefix(canon["host"], canon["path"]), kind, name))
    return reg


def edit_yaml(path, fn):
    doc = load_yaml(path)
    fn(doc)
    dump_yaml(doc, path)
    return doc


def problems_of(exc_info):
    return [(p.file, p.pointer, p.rule, p.message) for p in exc_info.value.problems]


def gates_ctx(**kw):
    """What the runner hands a gate. PROPOSED (G6): gates.Context."""
    from nanosoc_rm.t0 import gates
    return gates.Context.for_tests(**kw)


def member_client(config_path):
    """The web app, logged in as a member. PROPOSED (G5): create_app, FakeMembership."""
    from fastapi.testclient import TestClient
    from nanosoc_rm.auth import FakeMembership
    from nanosoc_rm.web import create_app
    return TestClient(create_app(config_path, auth=FakeMembership(login=MEMBER)))


def nightly_runs(window_hours=24):
    """This night's scheduled T0 results from the live service, by configuration."""
    rc, runs = rm_json("t0", "runs", "--json")
    assert rc in (0, 1), rc          # 1 = at least one run is red; still an answer
    cutoff = now_utc() - dt.timedelta(hours=window_hours)
    by_cfg = {}
    for obs in runs:
        f = obs["fact"]
        if f["run"]["trigger"] == "schedule" and utc(f["run"]["triggered_at"]) >= cutoff:
            by_cfg.setdefault(f["configuration"], []).append(f)
    return by_cfg


# --------------------------------------------------------------------------------------------
# Hermetic git fixtures for the D2 gates rc4 cannot exercise (coverage review High 2)
# --------------------------------------------------------------------------------------------
FX_WEB = "https://github.com/SoC-Labs/%s.git"
FX_DATE = "2026-09-30T12:00:00Z"


def _fx_env(extra=None):
    env = dict(os.environ, GIT_AUTHOR_NAME="rm-test", GIT_AUTHOR_EMAIL="rm-test@example.invalid",
               GIT_COMMITTER_NAME="rm-test", GIT_COMMITTER_EMAIL="rm-test@example.invalid",
               GIT_AUTHOR_DATE=FX_DATE, GIT_COMMITTER_DATE=FX_DATE, GIT_CONFIG_GLOBAL=os.devnull,
               GIT_CONFIG_NOSYSTEM="1", GIT_TERMINAL_PROMPT="0")
    env.update(extra or {})
    return env


def _fx_git(*args, env=None, input=None, check=True, cwd=None):
    p = subprocess.run(["git", *[str(a) for a in args]], env=env or _fx_env(), input=input, cwd=cwd,
                       capture_output=True, text=True, timeout=300)
    if check and p.returncode:
        raise RuntimeError("git %s: %s" % (" ".join(map(str, args)), p.stderr.strip()))
    return p


def fx_commit(repo, *, files=None, gitlinks=None, gitmodules=None, parents=(), ref=None):
    """One commit written with plumbing into bare `repo` (created on first use); moves `ref` if given."""
    repo = Path(repo)
    if not repo.exists():
        _fx_git("init", "-q", "--bare", repo)
    index = repo / "fx-index"
    if index.exists():
        index.unlink()
    env = _fx_env({"GIT_INDEX_FILE": str(index)})
    entries = dict(files or {})
    if gitmodules:
        entries[".gitmodules"] = "".join('[submodule "%s"]\n\tpath = %s\n\turl = %s\n' % (m, m, u)
                                         for m, u in gitmodules.items())
    args = []
    for name, text in sorted(entries.items()):
        blob = _fx_git("--git-dir", repo, "hash-object", "-w", "--stdin", env=env, input=text).stdout.strip()
        args += ["--cacheinfo", "100644,%s,%s" % (blob, name)]
    for name, sha in sorted((gitlinks or {}).items()):
        args += ["--cacheinfo", "160000,%s,%s" % (sha, name)]
    if args:
        _fx_git("--git-dir", repo, "update-index", "--add", *args, env=env)
    tree = _fx_git("--git-dir", repo, "write-tree", env=env).stdout.strip()
    sha = _fx_git("--git-dir", repo, "commit-tree", tree, *[a for p in parents for a in ("-p", p)], "-m", "fixture",
                  env=env).stdout.strip()
    if ref:
        _fx_git("--git-dir", repo, "update-ref", ref, sha)
    return sha


def fx_publish(src, dst, commit, branch="main"):
    """A second 'host' repo holding exactly the history of `commit` (objects reachable from it)."""
    if not Path(dst).exists():
        _fx_git("init", "-q", "--bare", dst)
    _fx_git("--git-dir", src, "push", "-q", str(dst), "%s:refs/heads/%s" % (commit, branch))


def fx_insteadof(url_map):
    """git -c options that route each fixture https URL to its local bare repo (raw-git oracle only)."""
    opts = ["-c", "protocol.file.allow=always"]
    for url, path in sorted(url_map.items()):
        opts += ["-c", "url.%s.insteadOf=%s" % (Path(path).resolve().as_uri(), url)]
    return opts


def hermetic_gate_fixtures(root, arm_specimen):
    """Good and broken inputs for t0.manifest.valid, t0.clone.upgrade-path, t0.vendor.no-arm-ip and
    t0.registry.schema. Returns {"cases": [(gate id, good ctx kwargs, bad ctx kwargs)], "url_map", "commits"}.
    Every https URL resolves through url_map to a local bare repo: no network (interfaces.md section 9)."""
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    bare = lambda name: root / ("%s.git" % name)
    url_map = {}

    def host(name):
        url_map[FX_WEB % name] = str(bare(name))
        return FX_WEB % name

    # A submodule with three commits; the "old host" of the bad pair serves only the first two.
    s1 = fx_commit(bare("fx-sub-src"), files={"rtl/sub.v": "module sub; endmodule\n"})
    s2 = fx_commit(bare("fx-sub-src"), files={"rtl/sub.v": "module sub(input a); endmodule\n"}, parents=[s1])
    s3 = fx_commit(bare("fx-sub-src"), files={"rtl/sub.v": "module sub(input a, b); endmodule\n"}, parents=[s2],
                   ref="refs/heads/main")
    fx_publish(bare("fx-sub-src"), bare("fx-sub-serving"), s3)   # old host that still serves every commit
    fx_publish(bare("fx-sub-src"), bare("fx-sub-frozen"), s2)    # old host frozen before s3
    fx_publish(bare("fx-sub-src"), bare("fx-sub-new"), s3)       # the new host
    old_ok, old_frozen, new = host("fx-sub-serving"), host("fx-sub-frozen"), host("fx-sub-new")

    def superproject(name, old_url):
        """upgrade_from pins s1 at old_url; commit moves the submodule to the new host and pins s3."""
        u1 = fx_commit(bare(name), files={"README": "v1\n"}, gitlinks={"sub": s1}, gitmodules={"sub": old_url})
        u2 = fx_commit(bare(name), files={"README": "v2\n"}, gitlinks={"sub": s3}, gitmodules={"sub": new},
                       parents=[u1], ref="refs/heads/main")
        host(name)
        return u1, u2

    ug1, ug2 = superproject("fx-upgrade-good", old_ok)      # a host move while the old host still serves: fix (C8)
    ub1, ub2 = superproject("fx-upgrade-bad", old_frozen)   # the old host lacks s3: an upgrade without sync fails

    manifest = (EX / "component-manifest.example.yaml").read_text()
    two_part = manifest.replace('version: "0.1.0"', 'version: "1.0"')
    assert two_part != manifest
    mg = fx_commit(bare("fx-manifest-good"), files={"nanosoc-component.yaml": manifest}, gitlinks={"sub": s2},
                   gitmodules={"sub": old_ok}, ref="refs/heads/main")
    mb = fx_commit(bare("fx-manifest-bad"), files={"nanosoc-component.yaml": two_part}, gitlinks={"sub": s2},
                   gitmodules={"sub": old_ok}, ref="refs/heads/main")
    host("fx-manifest-good"), host("fx-manifest-bad")

    vc = fx_commit(bare("fx-vendor-clean"), files={"rtl/top.v": "module top; endmodule\n"}, ref="refs/heads/main")
    vp = fx_commit(bare("fx-vendor-planted"), files={"rtl/top.v": "module top; endmodule\n",
                                                     "rtl/leaked.v": arm_specimen + "\nmodule leaked; endmodule\n"},
                   ref="refs/heads/main")
    host("fx-vendor-clean"), host("fx-vendor-planted")

    reg_good = example_registry(root / "registry-good")
    reg_bad = example_registry(root / "registry-bad")
    shutil.copyfile(BAD / "component-catalogue.no-class.yaml", reg_bad / "catalogue.yaml")

    def req(name, commit, upgrade_from=None):
        return {"configuration": "fx-" + name, "repo": {"host": "github", "path": "SoC-Labs/" + name},
                "commit": commit, "upgrade_from": upgrade_from, "canonical_urls": {}}

    ctx = lambda request: {"request": request, "url_map": dict(url_map)}
    cases = [
        ("t0.manifest.valid", ctx(req("fx-manifest-good", mg)), ctx(req("fx-manifest-bad", mb))),
        ("t0.clone.upgrade-path", ctx(req("fx-upgrade-good", ug2, ug1)), ctx(req("fx-upgrade-bad", ub2, ub1))),
        ("t0.vendor.no-arm-ip", ctx(req("fx-vendor-clean", vc)), ctx(req("fx-vendor-planted", vp))),
        ("t0.registry.schema", {"registry_dir": reg_good}, {"registry_dir": reg_bad}),
    ]
    return {"cases": cases, "url_map": url_map,
            "commits": {"s1": s1, "s2": s2, "s3": s3, "ug1": ug1, "ug2": ug2, "ub1": ub1, "ub2": ub2,
                        "mg": mg, "mb": mb, "vc": vc, "vp": vp},
            "registries": (reg_good, reg_bad)}


# --------------------------------------------------------------------------------------------
# Reference oracles (README section 6, interfaces.md section 10 Verify). They share no tool code:
# the self-checks use them to prove each negative control's input is what the AT claims.
# --------------------------------------------------------------------------------------------
MAY_SKIP = frozenset({"t0.manifest.valid", "t0.clone.upgrade-path"})


def reference_rollup(rows, declared):
    """README section 6 applied to verdicts.tsv rows: {gate: (verdict, control)}."""
    out = {}
    for gid in declared:
        main = [r for r in rows if r["gate"] == gid]
        ctl = [r for r in rows if r["gate"] == gid + ".negctl"]
        control = "missing" if not ctl else ("fired" if len(ctl) == 1 and ctl[0]["status"] == "PASS" else "not-fired")
        if len(main) != 1:
            verdict = "UNVERIFIED"
        else:
            status = main[0]["status"]
            verdict = {"PASS": "PASS" if control == "fired" else "UNVERIFIED", "FAIL": "FAIL",
                       "UNVERIFIED": "UNVERIFIED", "INFRA": "INFRA",
                       "WARN": "WARN" if control == "fired" else "UNVERIFIED",      # CR-9: WARN is amber
                       "SKIP": "NOT-RUN" if gid in MAY_SKIP else "UNVERIFIED"}[status]
        if verdict == "NOT-RUN":
            control = NOT_RUN_CONTROL
        out[gid] = (verdict, control)
    return out


def reference_run_verdict(items):
    """(run verdict, NOT-RUN count). PASS needs at least one PASS gate (README section 6). CR-9: a WARN gate
    counts as checked, and the ladder is FAIL > UNVERIFIED > INFRA > WAIVED > WARN > PASS."""
    verdicts = [v for v, _c in items.values()]
    not_run = verdicts.count("NOT-RUN")
    if "FAIL" in verdicts:
        return "FAIL", not_run
    if "UNVERIFIED" in verdicts or not ({"PASS", "WARN"} & set(verdicts)):
        return "UNVERIFIED", not_run
    if "INFRA" in verdicts:
        return "INFRA", not_run
    if "WAIVED" in verdicts:
        return "WAIVED", not_run
    if "WARN" in verdicts:
        return "WARN", not_run
    return "PASS", not_run


def reference_apply_waivers(rolled, items, waivers):
    """README section 6 "T0 waivers" on reference_rollup output: ({gate: (verdict, control)}, unused).
    An item is covered by a waiver naming its gate, tree path and result, or, when `not-reached`, by a
    covered item above it. Only a FAIL or UNVERIFIED gate whose control fired, with at least one non-pass
    item and every one covered, becomes WAIVED. A waiver matching no item at all is unused."""
    out, used = dict(rolled), set()
    for gid, (verdict, control) in rolled.items():
        bad = [i for i in items.get(gid, []) if i["result"] != "pass"]
        direct = {}
        for n, w in enumerate(waivers):
            for i in bad:
                if (w["gate"], w["tree_path"], w["result"]) == (gid, i["tree_path"], i["result"]):
                    direct[i["tree_path"]] = n
        used.update(direct.values())
        covered = 0
        for i in bad:
            above = [t for t in direct if i["tree_path"] == t
                     or (i["result"] == "not-reached" and i["tree_path"].startswith(t + "/"))]
            covered += bool(above)
        if verdict in ("FAIL", "UNVERIFIED") and control == "fired" and bad and covered == len(bad):
            out[gid] = ("WAIVED", control)
    return out, [w for n, w in enumerate(waivers) if n not in used]


def reference_verify(case, ce):
    """The first verify check (1..6) the case fails, or None (interfaces.md section 10)."""
    run, prov, pipeline, job, variables = case.run, case.prov, case.pipeline, case.job, case.variables
    if (pipeline["project_id"] != CI_PROJECT_ID or job["pipeline"]["project_id"] != CI_PROJECT_ID
            or pipeline.get("source") != "trigger" or pipeline["ref"] not in ("main",) or job["name"] != "t0"
            or pipeline["id"] != run["pipeline_id"] or job["runner"]["id"] not in (RUNNER_ID,)):
        return 1
    var = {v["key"]: v["value"] for v in variables}.get("RM_REQUEST")
    if var is None or var.encode() != run["request"]:
        return 2
    schemas, registry = ce.load_schemas()
    if list(ce.validator_for(schemas, registry, "result-bundle").iter_errors(prov)):
        return 3
    stored = json.loads(run["request"])
    if (prov["run_nonce"] != run["nonce"] or prov["request"] != {k: v for k, v in stored.items() if k not in ("schema", "nonce")}
            or prov["ci"]["pipeline_id"] != pipeline["id"] or prov["ci"]["job_id"] != job["id"]
            or prov["ci"]["harness_sha"] != pipeline["sha"]):
        return 4
    if prov["captured"]["superproject_commit"] != stored["commit"]:
        return 5
    lo = (utc(job["started_at"]) - dt.timedelta(seconds=60)).strftime("%Y-%m-%dT%H:%M:%SZ")
    hi = (utc(job["finished_at"]) + dt.timedelta(seconds=60)).strftime("%Y-%m-%dT%H:%M:%SZ")
    row_v = ce.validator_for(schemas, registry, "result-bundle", "/$defs/verdict_row")
    import tempfile
    with tempfile.NamedTemporaryFile("w", suffix=".tsv", delete=False) as fh:
        fh.write(case.verdicts)
    try:
        if ce.check_tsv(fh.name, row_v, run["nonce"], lo, hi):
            return 6
    finally:
        os.unlink(fh.name)
    return None


def job_env(root, name, env_changes=None, home_file=False, tmp_file=False):
    """A fresh job environment for AT-D10: HOME and TMPDIR directories, optionally with a leftover."""
    home, tmp = Path(root) / name / "home", Path(root) / name / "tmp"
    for d in (home, tmp):
        d.mkdir(parents=True)
    if home_file:
        (home / ".gitconfig").write_text("[user]\n\tname = left by a previous job\n")
    if tmp_file:
        (tmp / "previous-job.log").write_text("left by a previous job\n")
    env = {"CI_COMMIT_REF_PROTECTED": "true", "HOME": str(home), "TMPDIR": str(tmp), "USER": SERVICE_ACCOUNT}
    env.update(env_changes or {})
    return env


def reference_hardened(env, service_account):
    """What t0.runner.hardened checks, as names of the variables that break it (interfaces.md section 9)."""
    bad = []
    if env.get("CI_COMMIT_REF_PROTECTED") != "true":
        bad.append("CI_COMMIT_REF_PROTECTED")
    for var in ("HOME", "TMPDIR"):
        if any(Path(env[var]).iterdir()):
            bad.append(var)
    if env.get("USER") != service_account:
        bad.append("USER")
    bad += sorted(k for k in env if k.startswith("CI_") and ("TOKEN" in k or "PASSWORD" in k or k.startswith("CI_JOB_JWT")))
    return bad


def synthetic_netlists(root):
    """Two netlist files for SL-DEF-0001's D2 pattern: one with the defect's shape, one without."""
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    fired, clean = root / "fired.v", root / "clean.v"
    fired.write_text("module top;\n  cpu_ss u_cpu_ss (.HCLK(clk), .cpu_ss_hsel(UNCONNECTED_12), .HREADY(r));\nendmodule\n")
    clean.write_text("module top;\n  cpu_ss u_cpu_ss (.HCLK(clk), .cpu_ss_hsel(cpu_ss_hsel_w), .HREADY(r));\nendmodule\n")
    return fired, clean


# ============================================================================================
# AT-D1  Every tier has a command and a budget
# ============================================================================================
@pytest.mark.requirement("D1")
@pytest.mark.live
@NOT_BUILT
def test_AT_D1():
    """AT-D1: all 7 tiers have a budget; every configuration has a T0 command; every accepted
    nightly T0 finished inside the 2-minute budget."""
    from nanosoc_rm.registry import load_registry
    from nanosoc_rm.t0 import tiers                      # PROPOSED (G1)

    # Given: the registered configurations and the tier table
    reg = load_registry(Path(require_env("NANOSOC_RM_REGISTRY")))
    configs = [as_doc(c)["id"] for c in reg.configurations()]
    assert configs, "no registered configuration: nothing to check"

    # When / Then 1: exactly the seven tiers of the result-bundle `tier` enum
    tier_enum = json.loads((SCHEMAS / "result-bundle.schema.json").read_text())[
        "properties"]["tier"]["enum"]
    assert sorted(tiers.TIERS) == sorted(tier_enum)

    # Then 2: a budget per tier (D1's numbers), or null with a written reason
    assert tiers.validate_tiers(tiers.TIERS) == []
    assert tiers.TIERS["t0"].budget_s == 120            # "under 2 min"
    assert tiers.TIERS["t1"].budget_s == 600            # "~10 min"
    assert tiers.TIERS["t2"].budget_s == 900            # "5-15 min per SoC", upper end
    assert tiers.TIERS["t3"].budget_s == 10800          # "1-3 h", upper end
    assert tiers.TIERS["t6"].budget_s == 23400 and tiers.TIERS["t6"].manual   # "~6.5 h, manual"
    assert tiers.TIERS["t5"].manual
    for t in ("t4", "t5"):                              # D1 gives no number (G1)
        assert tiers.TIERS[t].budget_s is not None or tiers.TIERS[t].budget_reason

    # Then 3: T0 has a documented command for every configuration; CLI and function agree
    for cfg in configs:
        rc, rows = rm_json("tiers", cfg, "--json")
        assert rc == 0
        by_tier = {r["tier"]: r for r in rows}
        assert sorted(by_tier) == sorted(tier_enum)
        assert by_tier["t0"]["command"] == "nanosoc-rm t0 trigger %s" % cfg
        assert tiers.tier_commands(cfg)["t0"] == by_tier["t0"]["command"]

    # Then 4: every accepted scheduled T0 of the last 7 nights ran inside the T0 budget.
    # started_at / finished_at come from the D5-verified bundle (PROPOSED fields, G1).
    week = nightly_runs(window_hours=7 * 24)
    accepted = [f for fs in week.values() for f in fs if f["acceptance"] == "accepted"]
    assert accepted, "no accepted nightly T0 in 7 days: nothing measured"
    for f in accepted:
        b = tiers.check_budget("t0", f["started_at"], f["finished_at"])
        assert b.ok, "%s T0 took %ss, budget %ss" % (f["configuration"], b.seconds, b.budget_s)
        assert f.get("over_budget") is False


@pytest.mark.requirement("D1")
def test_AT_D1_negative_control():
    """AT-D1 control: an over-budget T0 is flagged; a tier with no budget fails validation."""
    from nanosoc_rm.t0 import tiers                      # PROPOSED (G1)
    prov = example_provenance()

    # Given: the example bundle, 02:03:30 -> 02:05:02 = 92 s (the pair's good half)
    ok = tiers.check_budget("t0", prov["started_at"], prov["finished_at"])
    assert ok.ok and ok.seconds == 92 and ok.budget_s == 120

    # When: finished_at moves to 02:06:31 (181 s). Then: over budget, never OK.
    over = tiers.check_budget("t0", prov["started_at"], "2026-10-01T02:06:31Z")
    assert not over.ok and over.seconds == 181 and over.budget_s == 120

    # When: t2 loses its budget and gives no reason. Then: validation fails, naming t2.
    broken = copy.deepcopy(tiers.TIERS)
    broken["t2"] = dataclasses.replace(broken["t2"], budget_s=None, budget_reason=None)
    problems = tiers.validate_tiers(broken)
    assert problems and any("t2" in p for p in problems), problems


# ============================================================================================
# AT-D2  T0 runs nightly on every configuration
# ============================================================================================
@pytest.mark.requirement("D2")
@pytest.mark.live
@NOT_BUILT
def test_AT_D2(tmp_path):
    """AT-D2: every configuration and @registry has tonight's accepted T0 report; rc4's raw gates
    go red on its known real items, and its registered waivers (GATE2 Q1) cover exactly those; every
    pin is judged on its canonical host (TideLink drift, keep tags)."""
    from nanosoc_rm.registry import load_registry
    from nanosoc_rm.t0 import gates

    # Given: the live registry and one completed night
    reg = load_registry(Path(require_env("NANOSOC_RM_REGISTRY")))
    expected = sorted([as_doc(c)["id"] for c in reg.configurations()] + ["@registry"])
    canonical = {as_doc(c)["id"]: as_doc(c)["canonical"]["host"] for c in reg.components()}
    canonical_repo = {as_doc(c)["id"]: as_doc(c)["canonical"] for c in reg.components()}
    # CR-2: which rc4 pins are unreachable on their canonical host is computed below from the catalogue and
    # an anonymous fetch per pin (Then 4), never hard-coded: the 2026-10-01 keep tags and DEC6 moved it.
    web = {h: v["web"].rstrip("/") for h, v in private_config()["hosts"].items()}

    # When: the night's runs are read
    by_cfg = nightly_runs()

    # Then 1: exactly one accepted scheduled run per configuration, plus @registry
    assert sorted(by_cfg) == expected
    for cfg, fs in by_cfg.items():
        assert len(fs) == 1, "%s: %d scheduled runs tonight" % (cfg, len(fs))
        assert fs[0]["acceptance"] == "accepted", (cfg, fs[0]["acceptance"], fs[0]["reason"])

    # Then 2: every D2 check is a declared gate, and every gate reports its control
    assert {g.id for g in gates.GATES.values() if g.scope == "configuration"} == set(CONFIG_GATES)
    assert {g.id for g in gates.GATES.values() if g.scope == "registry"} == set(REGISTRY_GATES)
    for cfg, (f,) in by_cfg.items():
        want = REGISTRY_GATES if cfg == "@registry" else CONFIG_GATES
        assert sorted(g["id"] for g in f["gates"]) == sorted(want), cfg
        for g in f["gates"]:   # D6: every gate that ran fired its control; only a NOT-RUN gate has none
            want_ctl = NOT_RUN_CONTROL if g["verdict"] == "NOT-RUN" else "fired"
            assert g["control"] == want_ctl, (cfg, g["id"], g["verdict"], g["control"])
    # eth-main declares upgrade_from, so its upgrade-path gate really runs (it cannot hide as NOT-RUN)
    if "eth-main" in by_cfg:
        main_gates = {x["id"]: x for x in by_cfg["eth-main"][0]["gates"]}
        assert main_gates["t0.clone.upgrade-path"]["verdict"] != "NOT-RUN"

    # Then 3: eth-rc4 matches the golden rows, for the real reasons
    (rc4,) = by_cfg["eth-rc4"]
    assert rc4["commit"] == RC4
    g = {x["id"]: x for x in rc4["gates"]}
    raw = lambda gid: g[gid].get("raw_verdict", g[gid]["verdict"])     # before waivers (README section 6)
    assert raw("t0.infra.probe") == "PASS"
    assert raw("t0.pins.fetchable") == "FAIL"
    assert raw("t0.urls.no-ssh-public") == "FAIL"
    assert raw("t0.manifest.valid") == "NOT-RUN"
    assert raw("t0.clone.upgrade-path") == "NOT-RUN"      # eth-rc4 has no upgrade_from
    assert raw("t0.vendor.no-arm-ip") == "UNVERIFIED"     # 7 trees never fetched
    nonce = rc4["run"]["nonce"]

    # Then 3b (GATE2 Q1): the registered waivers turn exactly the fully covered gates WAIVED; any item
    #   they do not cover keeps its gate red; a waiver that covered nothing has raised waiver_unused.
    rc4_cfg = {as_doc(c)["id"]: as_doc(c) for c in reg.configurations()}["eth-rc4"]
    live_items = {gid: gate_items(nonce, gid) for gid in RC4_WAIVED_GATES}
    want, unused = reference_apply_waivers({gid: (raw(gid), x["control"]) for gid, x in g.items()},
                                           live_items, rc4_cfg.get("t0_waivers", []))
    assert {gid: (x["verdict"], x["control"]) for gid, x in g.items()} == want
    assert rc4["verdict"] == reference_run_verdict(want)[0]
    assert rc4["verdict"] in ("FAIL", "WAIVED"), "rc4's frozen tree has known failures: never PASS"
    _rc, open_alerts = rm_json("alerts", "--json")
    for u in unused:                      # alert observations (interfaces.md section 8): fact.message names both
        assert any(a["fact"]["alert_kind"] == "waiver_unused" and u["gate"] in a["fact"]["message"]
                   and u["tree_path"] in a["fact"]["message"] for a in open_alerts), ("no waiver_unused alert", u)

    fetch = gate_items(nonce, "t0.pins.fetchable")
    by_result = {}
    for i in fetch:
        by_result.setdefault(i["result"], set()).add(i["tree_path"])
    assert by_result.get("fail") == set(RC4_SSH_PINS)
    assert len(by_result.get("not-reached", ())) == RC4_BELOW_TIDELINK
    assert all(p.startswith("tidelink/") for p in by_result["not-reached"])
    assert len(by_result.get("pass", ())) == RC4_PIN_COUNT - len(RC4_SSH_PINS) - RC4_BELOW_TIDELINK

    # Then 4: t0.pins.reachable on rc4
    reach = {i["tree_path"]: i for i in gate_items(nonce, "t0.pins.reachable")}
    # CR-2: the independent oracle. A pin of a public canonical repo that an anonymous depth-1 fetch of its
    # canonical https URL cannot get is unreachable there; the private toolkit is never judged (unverified).
    expected_unreachable = set()
    for tp, i in reach.items():
        if tp == "ASIC/asic-toolkit" or i["component"] in RC4_EXTERNALS:
            continue
        repo = canonical_repo[i["component"]]
        url = "%s/%s.git" % (web[repo["host"]], repo["path"])
        if not anon_fetchable(tmp_path / ("reach-%d" % len(expected_unreachable)) / tp.replace("/", "_"), url, i["commit"]):
            expected_unreachable.add(tp)
    # FAIL while a pin is off its canonical host; else UNVERIFIED (asic-toolkit is private)
    assert raw("t0.pins.reachable") == ("FAIL" if expected_unreachable else "UNVERIFIED")
    # All 44 pins are judged, including the 5 below tidelink: the gate walks by canonical https
    # URLs, not by the pinned ssh URL. PROPOSED (G15).
    assert len(reach) == RC4_PIN_COUNT
    for tp, i in reach.items():                          # judged on the canonical host, never "any host"
        want = "github" if i["component"] in RC4_EXTERNALS else canonical[i["component"]]
        assert i["host"] == want, (tp, i["host"])
    # TideLink host drift: 5e8bdb5 is v0.90 on GitHub (canonical) and missing on the GitLab copy.
    assert reach["tidelink"]["commit"] == TIDELINK_PIN
    assert (reach["tidelink"]["result"], reach["tidelink"]["host"]) == ("pass", "github")
    assert reach["nanosoc-multicore-system/ahb_qspi"]["commit"] == AHB_QSPI_GITLAB_ONLY
    assert reach["ASIC/asic-flows"]["commit"] == ASIC_FLOW_GITHUB_ONLY
    by_reach = {}
    for tp, i in reach.items():
        by_reach.setdefault(i["result"], set()).add(tp)
    assert by_reach.get("fail", set()) == expected_unreachable
    assert by_reach.get("unverified", set()) == {"ASIC/asic-toolkit"}   # canonical repo is private
    assert by_reach.get("not-reached", set()) == set()
    # Keep tags: the 11 formerly branch-only commits are reachable from a tag (or, for arch_tech
    # ecf76aa on GitHub, a branch), so none of them fails.
    assert reach["nanosoc-multicore-system/nanosoc_arch_tech"]["commit"] == ARCH_TECH_GITHUB_BRANCH_ONLY
    for tp in RC4_KEEP_TAG_PINS:
        if tp != "ASIC/asic-toolkit":
            assert reach[tp]["result"] == "pass", (tp, reach[tp])

    ssh = gate_items(nonce, "t0.urls.no-ssh-public")
    assert {i["tree_path"] for i in ssh if i["result"] == "fail"} == set(RC4_SSH_PINS)

    # Then 5: the registry job judges every cited commit; the unpushed bench fails while unpushed
    (regrun,) = by_cfg["@registry"]
    refs = gate_items(regrun["run"]["nonce"], "t0.registry.refs-resolve")
    sl1 = {as_doc(x)["id"]: as_doc(x) for x in reg.defects()}["SL-DEF-0001"]
    bench_commit = (sl1.get("regression_test") or {}).get("commit")
    assert bench_commit, "SL-DEF-0001 records no bench: F1 needs one pinned by commit"
    bench = [i for i in refs if i["commit"] == bench_commit]
    assert bench, "the registry job did not judge SL-DEF-0001's bench %s" % bench_commit[:7]
    pushed = anon_fetchable(tmp_path / "oracle", ETH_HTTPS, bench_commit)
    assert bench[0]["result"] == ("pass" if pushed else "fail")
    rg = {x["id"]: x for x in regrun["gates"]}
    any_fail = any(i["result"] == "fail" for i in refs)
    assert rg["t0.registry.refs-resolve"]["verdict"] == ("FAIL" if any_fail else "PASS")

    # Then 6: the dashboard shows every configuration with tonight's verdict, data age < 24 h
    html = member_client(Path(require_env("NANOSOC_RM_CONFIG"))).get("/").text
    rows = {a["data-configuration"]: a for a in elements_with(html, "data-configuration")}
    assert sorted(rows) == sorted(c for c in expected if c != "@registry")
    for cfg, a in rows.items():
        assert a["data-verdict"] == by_cfg[cfg][0]["verdict"]
        assert int(a["data-age-seconds"]) < 24 * 3600


@pytest.mark.requirement("D2")
def test_AT_D2_negative_control(tmp_path, monkeypatch):
    """AT-D2 control: the CI API refuses eth-main's nightly trigger. eth-main must show a
    missed night, never last night's result and never PASS."""
    from nanosoc_rm import db
    from nanosoc_rm.ci.client import FakeCI              # PROPOSED (G4)
    from nanosoc_rm.ci.trigger import trigger

    # Given: the example registry (eth-rc4, eth-main, multicore-master) and last night's green
    cfg_path = write_config(tmp_path, monkeypatch)
    example_registry(tmp_path / "registry")
    prime_for_trigger(tmp_path)                      # CR-3: a git registry and primed mirrors
    last_night = {
        "schema": 1, "kind": "t0_result",
        "subject": "t0_result:eth-main:" + "a" * 32, "observed_at": "2026-09-30T02:06:00Z",
        "sources": [{"kind": "ci_api", "command": "GET projects/%d/jobs/1999/artifacts" % CI_PROJECT_ID}],
        "fact": {"configuration": "eth-main", "commit": "8e6d0434fc8331191607ad5b2af73646e995c892",
                 "run": {"nonce": "a" * 32, "trigger": "schedule", "triggered_by": "schedule",
                         "triggered_at": "2026-09-30T02:00:05Z", "pipeline_id": 999, "job_id": 1999},
                 "acceptance": "accepted", "reason": None, "verdict": "PASS",
                 "gates": [{"id": gid, "verdict": "PASS", "control": "fired", "detail": ""}
                           for gid in CONFIG_GATES],
                 "bundle": {"store": "ci", "path": "jobs/1999/artifacts"},
                 "started_at": "2026-09-30T02:00:40Z", "finished_at": "2026-09-30T02:02:10Z",
                 "over_budget": False, "runner": {"id": RUNNER_ID, "description": "rm-hardened",
                                                  "tags": ["rm-hardened"]}},
    }
    db.put(last_night)

    # When: tonight's schedule fires and the trigger for eth-main gets HTTP 500
    fake = FakeCI(fail_trigger_for={"eth-main": 500})
    out = {c: as_doc(trigger(c, by="schedule", kind="schedule", client=fake))
           for c in ("eth-rc4", "eth-main", "multicore-master", "@registry")}

    # Then: eth-main's night is recorded as missed: no bundle, not PASS, reason names the trigger
    f = out["eth-main"]["fact"]
    assert f["acceptance"] == "no-bundle"
    assert f["verdict"] in ("UNVERIFIED", "INFRA")
    assert "trigger" in f["reason"] and "500" in f["reason"]
    for c in ("eth-rc4", "multicore-master", "@registry"):
        assert out[c]["fact"]["acceptance"] == "pending", c

    # Then: the nightly coverage check names eth-main and exits 1 (PROPOSED, G2)
    cp = rm("t0", "coverage", "--night", now_utc().date().isoformat(), "--json")
    assert cp.returncode == 1, cp.stdout
    missing = [json.loads(line) for line in cp.stdout.splitlines() if line.strip()]
    assert "eth-main" in {m["configuration"] for m in missing}

    # Then: the dashboard does not show last night's PASS as eth-main's current result
    html = member_client(cfg_path).get("/").text
    row = {a["data-configuration"]: a for a in elements_with(html, "data-configuration")}["eth-main"]
    assert row["data-verdict"] != "PASS"

    # Part 2 (coverage review High 2). rc4 cannot exercise three D2 checks (manifest and upgrade path
    # are NOT-RUN, the Arm scan is UNVERIFIED) and nothing real exercises the registry schema gate.
    # Each is shown to PASS on a good hermetic input and FAIL on a broken one, through the test-only
    # url_map seam (interfaces.md section 9), with its own control firing in the same run.
    from nanosoc_rm.scan import rules
    from nanosoc_rm.t0 import gates
    fx = hermetic_gate_fixtures(tmp_path / "gatefx", arm_specimen=rules.specimen("arm.banner"))
    for gid, good, bad in fx["cases"]:
        row, ctl = gates.run_with_control(gid, gates_ctx(nonce="1" * 32, workdir=tmp_path / ("w-" + gid), **good))
        assert (row["status"], ctl["status"]) == ("PASS", "PASS"), (gid, row["detail"], ctl["detail"])
        row, _ = gates.run_with_control(gid, gates_ctx(nonce="2" * 32, workdir=tmp_path / ("b-" + gid), **bad))
        assert row["status"] == "FAIL", (gid, row["detail"])


# ============================================================================================
# AT-D5  Results are pulled from CI and checked
# ============================================================================================
def _other_project(c):
    c.pipeline["project_id"] = c.job["pipeline"]["project_id"] = OTHER_PROJECT_ID


def _feature_ref(c):
    c.pipeline["ref"] = c.job["ref"] = c.job["pipeline"]["ref"] = "feat/x"


def _job_name(c):
    c.job["name"] = "t0-debug"


def _pipeline_id(c):
    c.run["pipeline_id"] = 999


def _request_variable(c):
    c.variables[0]["value"] = c.variables[0]["value"].replace(RC4, RC4_PARENT)


def _no_nonce(c):
    del c.prov["run_nonce"]           # as schemas/examples/invalid/result-bundle.provenance-without-nonce.yaml


def _other_nonce(c):
    c.prov["run_nonce"] = OTHER_RUN_NONCE


def _request_changed(c):
    c.prov["request"]["upgrade_from"] = RC4_PARENT


def _harness(c):
    c.prov["ci"]["harness_sha"] = "0" * 39 + "2"


def _four_columns(c):
    c.verdicts = (BAD / "result-bundle.verdicts.four-columns.tsv").read_text()


def _foreign_nonce(c):
    c.verdicts = (BAD / "result-bundle.verdicts.foreign-nonce.tsv").read_text()


def _early_row(c):
    rows = parse_rows(c.verdicts)
    rows[0]["utc"] = "2026-10-01T02:01:00Z"     # before the job started (02:03:28)
    c.verdicts = format_rows(rows)


def _empty_rows(c):
    c.verdicts = ""


# (label, the verify check that owns the field, mutation). interfaces.md section 10, Verify.
D5_TAMPERS = (
    ("pipeline from another project", 1, _other_project),
    ("ref not in allowed_refs", 1, _feature_ref),
    ("job name t0-debug", 1, _job_name),
    ("pipeline id differs from the run's", 1, _pipeline_id),
    ("RM_REQUEST differs by one commit", 2, _request_variable),
    ("provenance without run_nonce", 3, _no_nonce),
    ("run_nonce of another run", 4, _other_nonce),
    ("provenance request differs", 4, _request_changed),
    ("harness_sha differs from the API", 4, _harness),
    ("verdicts.tsv with 4 columns", 6, _four_columns),
    ("row with a foreign nonce", 6, _foreign_nonce),
    ("row before the job started", 6, _early_row),
    ("empty verdicts.tsv", 6, _empty_rows),
)


@pytest.mark.requirement("D5")
def test_AT_D5(tmp_path, monkeypatch):
    """AT-D5: the genuine rc4 bundle is accepted; each single-field tamper is rejected by the
    check that owns the field; no route accepts a pushed result."""
    from nanosoc_rm import db
    cfg_path = write_config(tmp_path, monkeypatch)

    # Given: the stored run and the CI API's records for it. When: verify(). Then 1: accepted.
    v = run_verify(genuine_case(), tmp_path / "genuine")
    assert v.acceptance == "accepted", v.reason
    assert v.check is None

    # Then 2: every other tamper is rejected, at the check that owns the field
    for n, (label, check, mutate) in enumerate(D5_TAMPERS):
        c = genuine_case()
        mutate(c)
        v = run_verify(c, tmp_path / ("tamper-%02d" % n))
        assert v.acceptance == "rejected", label
        assert v.check == check, (label, v.check, v.reason)
        assert v.reason, label

    # Then 3: no push path. The only non-GET routes are the two Run T0 routes (section 13).
    # CR-3: the trigger gets a registry checkout, so it reaches the CI POST and fails there (no CI host).
    example_registry(tmp_path / "registry")
    prime_for_trigger(tmp_path)
    client = member_client(cfg_path)
    writes = {(m, r.path) for r in client.app.routes
              for m in getattr(r, "methods", ()) if m not in ("GET", "HEAD", "OPTIONS")}
    assert writes == {("POST", "/configurations/{id}/runs"),
                      ("POST", "/api/v1/configurations/{id}/runs")}
    posted = example_provenance()
    for path in ("/configurations/eth-rc4/runs", "/api/v1/configurations/eth-rc4/runs"):
        client.post(path, json=posted)
    results = [as_doc(o)["fact"] for o in db.latest("t0_result", "t0_result:")]
    # Each POST made a run (pending, or no-bundle when the trigger cannot reach a CI host); none of
    # them is the posted bundle. An empty list would prove nothing, so it fails.
    assert len(results) >= 2, results
    assert all(r["run"]["nonce"] != posted["run_nonce"] for r in results)
    assert all(r["acceptance"] != "accepted" for r in results)


@pytest.mark.requirement("D5")
def test_AT_D5_negative_control(tmp_path, monkeypatch):
    """AT-D5 control (the done-when): the genuine bundle with only the captured superproject
    commit changed to rc4's real parent is rejected, and stored as UNVERIFIED."""
    write_config(tmp_path, monkeypatch)

    # Given: the genuine case, with one field changed
    c = genuine_case()
    c.prov["captured"]["superproject_commit"] = RC4_PARENT

    # When: verify(). Then: rejected at check 5, naming the field and both commits
    v = run_verify(c, tmp_path / "direct")
    assert v.acceptance == "rejected"
    assert v.check == 5, v.reason
    assert "superproject_commit" in v.reason and RC4 in v.reason and RC4_PARENT in v.reason

    # Then: end to end, the stored t0_result is rejected, UNVERIFIED and holds no bundle
    c = genuine_case()
    c.prov["captured"]["superproject_commit"] = RC4_PARENT
    f = fetch_via_fake_ci(c, tmp_path / "e2e")
    assert (f["acceptance"], f["verdict"], f["bundle"]) == ("rejected", "UNVERIFIED", None)
    assert RC4_PARENT in f["reason"]


# ============================================================================================
# AT-D6  Every gate carries a negative control
# ============================================================================================
def rc4_parents():
    """rc4's superproject as the no-ssh gate sees it: one public parent and its .gitmodules."""
    return {"%s/%s@%s" % (ETH_REPO[0], ETH_REPO[1], RC4): {"public": True, "gitmodules": RC4_GITMODULES}}


def _rc4_ctx(tmp_path, nonce):
    return gates_ctx(nonce=nonce, workdir=tmp_path / "work", parents=rc4_parents())


@pytest.mark.requirement("D6")
def test_AT_D6(tmp_path, monkeypatch):
    """AT-D6: every declared gate is reported with its control and whether it fired; a PASS
    never stands without a fired control."""
    from nanosoc_rm import db
    from nanosoc_rm.t0 import gates                     # Gate, GATES, rollup: PROPOSED (G6)
    cfg_path = write_config(tmp_path, monkeypatch)
    prov = example_provenance()
    nonce = prov["run_nonce"]

    # Then 1: every gate in the table has a control, in both scopes
    assert {g.id for g in gates.GATES.values()} == set(CONFIG_GATES) | set(REGISTRY_GATES)
    for g in gates.GATES.values():
        assert callable(g.check) and callable(g.control), g.id

    # Then 5: the no-ssh gate and its control run in one run on rc4's real .gitmodules
    row, ctl = gates.run_with_control("t0.urls.no-ssh-public", _rc4_ctx(tmp_path, nonce))
    assert row["status"] == "FAIL"
    for p in RC4_SSH_PINS:
        assert p in row["detail"]
    assert "tidechart" not in row["detail"] and "ASIC/asic-flows" not in row["detail"]
    assert (ctl["gate"], ctl["status"]) == ("t0.urls.no-ssh-public.negctl", "PASS")
    assert row["nonce"] == ctl["nonce"] == nonce

    # When: the golden rows are rolled up. Then 2, 3, 6.
    items = {i["id"]: i for i in gates.rollup(
        parse_rows(with_runner_hardened(example_verdicts(), nonce)), scope="configuration")}
    assert sorted(items) == sorted(CONFIG_GATES)
    assert {k: (v["verdict"], v["control"]) for k, v in items.items()} == EXPECTED_RC4_ROLLUP
    assert all(v["control"] == "fired" for v in items.values() if v["verdict"] == "PASS")
    assert all(v["detail"].strip() for v in items.values() if v["verdict"] == "NOT-RUN")  # README 6
    assert gates.run_verdict(list(items.values())) == ("FAIL", 2)

    # GATE2 Q1: rc4's waivers applied to the same roll-up and rc4's items. Only the four known
    # gates change, each keeps its raw verdict, and the inputs are untouched.
    rc4_items = example_items()
    waived, unused = gates.apply_waivers(list(items.values()), rc4_items, example_waivers())
    wv = {i["id"]: i for i in waived}
    assert {k: (v["verdict"], v["control"]) for k, v in wv.items()} == EXPECTED_RC4_WAIVED_ROLLUP
    assert unused == []
    for gid in RC4_WAIVED_GATES:
        assert wv[gid]["raw_verdict"] == EXPECTED_RC4_ROLLUP[gid][0]
        assert sorted(w["tree_path"] for w in wv[gid]["waived"]) == sorted(
            i["tree_path"] for i in rc4_items[gid] if i["result"] != "pass")
    assert gates.run_verdict(waived) == ("WAIVED", 2)
    assert {k: (v["verdict"], v["control"]) for k, v in items.items()} == EXPECTED_RC4_ROLLUP

    # The golden example carries the t0.runner.hardened rows (G6 fixed); without them the gate is
    # missing, and a missing gate rolls up UNVERIFIED, never PASS.
    raw = {i["id"]: i for i in gates.rollup(parse_rows(example_verdicts()), scope="configuration")}
    assert (raw["t0.runner.hardened"]["verdict"], raw["t0.runner.hardened"]["control"]) == ("PASS", "fired")
    stripped = [r for r in parse_rows(example_verdicts()) if not r["gate"].startswith("t0.runner.hardened")]
    gone = {i["id"]: i for i in gates.rollup(stripped, scope="configuration")}["t0.runner.hardened"]
    assert (gone["verdict"], gone["control"]) == ("UNVERIFIED", "missing")

    # Then 4: the run page shows each gate's control and whether it fired
    db.put({"schema": 1, "kind": "t0_result", "subject": "t0_result:eth-rc4:" + nonce,
            "observed_at": prov["finished_at"],
            "sources": [{"kind": "ci_api", "command": "GET projects/%d/jobs/%d/artifacts"
                         % (CI_PROJECT_ID, prov["ci"]["job_id"])}],
            "fact": {"configuration": "eth-rc4", "commit": RC4,
                     "run": {"nonce": nonce, "trigger": "schedule", "triggered_by": "schedule",
                             "triggered_at": "2026-10-01T02:03:10Z",
                             "pipeline_id": prov["ci"]["pipeline_id"], "job_id": prov["ci"]["job_id"]},
                     "acceptance": "accepted", "reason": None, "verdict": "FAIL",
                     "gates": list(items.values()),
                     "bundle": {"store": "ci", "path": "jobs/%d/artifacts" % prov["ci"]["job_id"]},
                     "started_at": prov["started_at"], "finished_at": prov["finished_at"], "over_budget": False,
                     "runner": {"id": RUNNER_ID, "description": "rm-hardened", "tags": ["rm-hardened"]}}})
    html = member_client(cfg_path).get("/runs/" + nonce).text
    shown = {a["data-gate"]: a for a in elements_with(html, "data-gate")}
    assert sorted(shown) == sorted(CONFIG_GATES)
    for gid, (verdict, control) in EXPECTED_RC4_ROLLUP.items():
        assert (shown[gid]["data-verdict"], shown[gid]["data-control"]) == (verdict, control), gid

    # Then 4b: the same run stored WAIVED shows each waived gate amber with its raw verdict, and every
    #   covered item with its waiver (README section 6; interfaces.md section 13)
    ex = json.loads((EX / "observation.t0-result-waived.example.jsonl").read_text())
    assert ex["subject"] == "t0_result:eth-rc4:" + nonce
    db.put(ex)
    html = member_client(cfg_path).get("/runs/" + nonce).text
    shown = {a["data-gate"]: a for a in elements_with(html, "data-gate")}
    for gid in RC4_WAIVED_GATES:
        assert (shown[gid]["data-verdict"], shown[gid].get("data-raw-verdict")) == (
            "WAIVED", EXPECTED_RC4_ROLLUP[gid][0]), gid
        assert "state-amber" in (shown[gid].get("class") or "").split(), gid
    assert {a["data-waived"] for a in elements_with(html, "data-waived")} == {
        w["tree_path"] for g in ex["fact"]["gates"] for w in g.get("waived", [])}


@pytest.mark.requirement("D6")
def test_AT_D6_negative_control(tmp_path, monkeypatch):
    """AT-D6 control: a gate that cannot fail is caught (its control does not fire, the gate is
    UNVERIFIED); missing, unfired and duplicated rows never show PASS."""
    from nanosoc_rm.t0 import gates
    nonce = example_provenance()["run_nonce"]

    # Given: the no-ssh gate replaced by a mutant whose check always passes
    gid = "t0.urls.no-ssh-public"
    mutant = dataclasses.replace(gates.GATES[gid],
                                 check=lambda ctx: ("PASS", "mutant: this check cannot fail"))
    monkeypatch.setitem(gates.GATES, gid, mutant)

    # When: it runs with its own control, in one run, on rc4's real .gitmodules
    row, ctl = gates.run_with_control(gid, _rc4_ctx(tmp_path, nonce))

    # Then: the mutant passes rc4 (wrongly), its control does not fire, the gate is UNVERIFIED
    assert row["status"] == "PASS"
    assert ctl["status"] == "FAIL"
    item = {i["id"]: i for i in gates.rollup([row, ctl], scope="configuration")}[gid]
    assert (item["verdict"], item["control"]) == ("UNVERIFIED", "not-fired")

    # And the same holds for EVERY declared gate (coverage review Medium 1): replaced by an always-PASS
    # mutant, each gate's own control fails to fire, so none of them can show PASS. This proves each
    # control runs the gate's own check on a broken input, not a separate always-red function.
    registry = example_registry(tmp_path / "registry-d6")
    for other, gate in sorted(gates.GATES.items()):
        mutant = dataclasses.replace(gate, check=lambda ctx: ("PASS", "mutant: this check cannot fail"))
        monkeypatch.setitem(gates.GATES, other, mutant)
        ctx = gates_ctx(nonce=nonce, workdir=tmp_path / ("mut-" + other), registry_dir=registry,
                        parents=rc4_parents())
        row, ctl = gates.run_with_control(other, ctx)
        assert (row["status"], ctl["status"]) == ("PASS", "FAIL"), (other, row, ctl)
        rolled = {i["id"]: i for i in gates.rollup([row, ctl], scope=gate.scope)}[other]
        assert (rolled["verdict"], rolled["control"]) == ("UNVERIFIED", "not-fired"), other

    # Then: README section 6 rules, each on the golden with one change
    golden = parse_rows(with_runner_hardened(example_verdicts(), nonce))

    def rolled(rows):
        return {i["id"]: i for i in gates.rollup(rows, scope="configuration")}["t0.infra.probe"]

    no_ctl = [r for r in golden if r["gate"] != "t0.infra.probe.negctl"]
    assert (rolled(no_ctl)["verdict"], rolled(no_ctl)["control"]) == ("UNVERIFIED", "missing")

    unfired = copy.deepcopy(golden)
    next(r for r in unfired if r["gate"] == "t0.infra.probe.negctl")["status"] = "FAIL"
    assert (rolled(unfired)["verdict"], rolled(unfired)["control"]) == ("UNVERIFIED", "not-fired")

    twice = golden + [dict(next(r for r in golden if r["gate"] == "t0.infra.probe"))]
    assert rolled(twice)["verdict"] == "UNVERIFIED"

    # And GATE2 Q1: waivers never hide a new failure, never cover a control that did not fire, and
    #   report a waiver whose cause has gone (README section 6)
    base = gates.rollup(golden, scope="configuration")
    new = copy.deepcopy(example_items())
    new["t0.pins.reachable"].append(dict(NEW_FAILURE))
    # CR-2: today's reachable row is UNVERIFIED; a job that saw the new failure writes FAIL for the gate
    failed = [dict(g, verdict="FAIL") if g["id"] == "t0.pins.reachable" else g for g in base]
    w, _ = gates.apply_waivers(failed, new, example_waivers())
    reach = {i["id"]: i for i in w}["t0.pins.reachable"]
    assert reach["verdict"] == "FAIL" and "waived" not in reach
    assert gates.run_verdict(w)[0] == "FAIL"

    below = copy.deepcopy(example_items())     # a FAIL below a waived pin is new, not "not reached"
    next(i for i in below["t0.pins.fetchable"] if i["tree_path"] == "tidelink/deps/tidelink-phy").update(
        result="fail", why="SYNTHETIC: a failure of its own, not caused by tidelink")
    w, _ = gates.apply_waivers(base, below, example_waivers())
    assert {i["id"]: i for i in w}["t0.pins.fetchable"]["verdict"] == "FAIL"

    unfired = copy.deepcopy(golden)
    next(r for r in unfired if r["gate"] == "t0.pins.fetchable.negctl")["status"] = "FAIL"
    w, _ = gates.apply_waivers(gates.rollup(unfired, scope="configuration"), example_items(), example_waivers())
    fetch = {i["id"]: i for i in w}["t0.pins.fetchable"]
    assert (fetch["verdict"], fetch["control"]) == ("FAIL", "not-fired") and "waived" not in fetch

    # CR-2: e908ac8 reached GitHub (GATE2 Q2), so today's items no longer hold it; the old waiver is unused
    w, unused = gates.apply_waivers(base, example_items(), example_waivers() + [RETIRED_AHB_QSPI_WAIVER])
    assert {i["id"]: i for i in w}["t0.pins.reachable"]["verdict"] == "WAIVED"     # the toolkit is still private
    assert [(u["gate"], u["tree_path"]) for u in unused] == [("t0.pins.reachable", AHB_QSPI_TREE_PATH)]


# ============================================================================================
# AT-D10  T0 runs on the hardened runner
# ============================================================================================
@pytest.mark.requirement("D10")
@pytest.mark.live
@NOT_BUILT
def test_AT_D10():
    """AT-D10: the real T0 job passes t0.runner.hardened; the runner's id and settings are
    recorded and match the expected ones."""
    cfg = private_config()
    allowed = set(cfg["ci"]["allowed_runner_ids"])
    assert allowed, "no allowed runner configured: nothing to check against"

    # Given / When: tonight's accepted scheduled runs
    by_cfg = nightly_runs()
    accepted = [fs[0] for c, fs in by_cfg.items() if c != "@registry"
                and fs[0]["acceptance"] == "accepted"]
    assert accepted, "no accepted nightly T0 tonight"

    # Then 1: the in-job gate passed and its control fired, in every run
    for f in accepted:
        g = {x["id"]: x for x in f["gates"]}["t0.runner.hardened"]
        assert (g["verdict"], g["control"]) == ("PASS", "fired"), f["configuration"]

    # Then 2-4: runner settings recorded, fresh, allowed and as expected. PROPOSED (G7).
    rc, obs = rm_json("runner", "--json")
    assert rc == 0, "runner settings drifted or were not recorded"
    (s,) = [o for o in obs if o["kind"] == "runner_settings"]
    fact = s["fact"]
    assert fact["runner_id"] in allowed
    for key, want in EXPECTED_RUNNER_SETTINGS.items():
        assert fact["settings"][key] == want, key
    assert cfg["ci"]["runner_tag"] in fact["settings"]["tag_list"]
    assert fact["drift"] == []
    newest_run = max(utc(f["run"]["triggered_at"]) for f in accepted)
    assert utc(s["observed_at"]) >= newest_run, "runner settings older than tonight's run"


@pytest.mark.requirement("D10")
def test_AT_D10_negative_control(tmp_path, monkeypatch):
    """AT-D10 control: one-variable changes to a good job environment each FAIL the gate; a job
    run on a runner outside the allowlist is rejected."""
    from nanosoc_rm.t0 import gates
    gate = gates.GATES["t0.runner.hardened"]

    def ctx(name, env_changes=None, home_file=False, tmp_file=False):
        """A fresh job environment per case, so one case's leftovers never leak into another."""
        env = job_env(tmp_path, name, env_changes, home_file, tmp_file)
        return gates_ctx(env=env, service_account=SERVICE_ACCOUNT, nonce="0" * 32,
                         workdir=tmp_path / name / "work")

    # Given: the good environment passes, and the contracted in-run control fires
    status, detail = gate.check(ctx("good"))
    assert status == "PASS", detail
    row, ctl = gates.run_with_control(gate.id, ctx("good-with-control"))
    assert (row["status"], ctl["status"]) == ("PASS", "PASS")

    # When / Then: each single change fails, and the detail names what changed
    for name, c in (("CI_COMMIT_REF_PROTECTED", ctx("ref", {"CI_COMMIT_REF_PROTECTED": "false"})),
                    ("HOME", ctx("home", home_file=True)),
                    ("TMPDIR", ctx("tmpdir", tmp_file=True)),
                    ("USER", ctx("user", {"USER": "jbloggs"})),
                    ("CI_JOB_TOKEN", ctx("token", {"CI_JOB_TOKEN": "left-in-the-environment"}))):
        status, detail = gate.check(c)
        assert status == "FAIL", name
        assert name in detail, (name, detail)

    # PROPOSED (G7): the genuine bundle from a runner outside the allowlist is rejected at check 1
    write_config(tmp_path, monkeypatch)
    case = genuine_case()
    case.job["runner"]["id"] = RUNNER_ID + 1
    v = run_verify(case, tmp_path / "foreign-runner")
    assert (v.acceptance, v.check) == ("rejected", 1)
    assert str(RUNNER_ID + 1) in v.reason


# ============================================================================================
# AT-D11  Bundles are scanned for vendor content
# ============================================================================================
@pytest.mark.requirement("D11")
@pytest.mark.live
@NOT_BUILT
def test_AT_D11(tmp_path, monkeypatch):
    """AT-D11: the genuine rc4 bundle scans clean, is accepted and stored; the arm.banner rule
    fires on one real vendor file (realism, lab only)."""
    from nanosoc_rm.scan import scan_dir, scan_paths      # scan_paths: PROPOSED (G8)
    write_config(tmp_path, monkeypatch)

    # Given: the genuine bundle. When: the service's own scan.
    b = write_bundle(tmp_path / "scan", example_provenance(),
                     with_runner_hardened(example_verdicts(), example_provenance()["run_nonce"]))
    r = scan_dir(b)

    # Then 1: clean, with a real census and every rule armed first
    assert r.verdict == "clean", r.findings
    assert r.files_scanned >= 3
    assert "arm.banner" in r.armed and all(r.armed.values()), r.armed

    # Then 2: accepted and stored, end to end
    f = fetch_via_fake_ci(genuine_case(), tmp_path / "e2e")
    assert f["acceptance"] == "accepted"
    assert f["bundle"] is not None and f["bundle"]["store"] == "ci"

    # Then 3: realism. The rule fires on a real vendor file, read in place; locations only.
    ref = Path(require_env("RM_ARM_REFERENCE_FILE"))
    assert ref.is_file()
    rr = scan_paths([ref])
    hits = [h for h in rr.findings if h["rule"] == "arm.banner"]
    assert hits and hits[0]["line"] <= 30
    assert set(hits[0]) == {"rule", "file", "line"}       # never the matched text


@pytest.mark.requirement("D11")
def test_AT_D11_negative_control(tmp_path, monkeypatch):
    """AT-D11 control (the done-when): the same bundle with a planted Arm banner in its log is
    refused, never stored; an empty bundle is refused, not accepted."""
    from nanosoc_rm.scan import rules, scan_dir           # rules.specimen: PROPOSED (G8)
    write_config(tmp_path, monkeypatch)
    quarantine = tmp_path / "data" / "quarantine"

    # Given: the scanner's own invented arm.banner specimen, planted at logs/t0.log line 3.
    # The job's own scan still claims clean, as a job that missed it would.
    specimen = rules.specimen("arm.banner")
    planted = {"logs/t0.log": T0_LOG + specimen}
    assert example_provenance()["scan"]["verdict"] == "clean"

    # When: the service scans. Then: a finding at that location, reported without its text.
    r = scan_dir(write_bundle(tmp_path / "scan", example_provenance(), example_verdicts(), planted))
    assert r.verdict == "finding"
    assert [h for h in r.findings if h["rule"] == "arm.banner"] == [
        {"rule": "arm.banner", "file": "logs/t0.log", "line": 3}]
    first_line = specimen.strip().splitlines()[0]
    assert first_line not in json.dumps(r.findings)

    # Then: end to end, refused at check 7, UNVERIFIED, nothing stored, quarantine emptied
    f = fetch_via_fake_ci(genuine_case(), tmp_path / "e2e", files=planted)
    assert (f["acceptance"], f["verdict"], f["bundle"]) == ("refused", "UNVERIFIED", None)
    assert "arm.banner" in f["reason"] and "logs/t0.log:3" in f["reason"]
    assert first_line not in f["reason"]
    # Nothing holding the planted text survives anywhere under the service's data dir, wherever the
    # tool chose to quarantine (a check on one directory name alone could pass vacuously).
    assert quarantine.is_dir()
    leftovers = [str(q) for q in (tmp_path / "data").rglob("*")
                 if q.is_file() and first_line in q.read_text(errors="replace")]
    assert leftovers == [], leftovers

    # Then: a bundle with nothing to scan is unverified, and so refused
    empty = tmp_path / "empty" / "rm-bundle"
    empty.mkdir(parents=True)
    e = scan_dir(empty)
    assert e.verdict == "unverified" and e.files_scanned == 0

    # Then, job side ("before upload"): the runner's last step drops the flagged file and records
    # only its location, because the job uploads rm-bundle/ with `when: always`. PROPOSED (G8).
    from nanosoc_rm.t0.runner import finalise_bundle
    job_side = write_bundle(tmp_path / "job", example_provenance(), example_verdicts(), planted)
    finalise_bundle(job_side)
    assert not (job_side / "logs" / "t0.log").exists()
    scan = json.loads((job_side / "provenance.json").read_text())["scan"]
    assert scan["verdict"] == "finding"
    assert first_line not in json.dumps(scan)
    assert {"rule": "arm.banner", "file": "logs/t0.log", "line": 3} in scan["findings"]   # CR-3: {rule, file, line}


# ============================================================================================
# AT-F1  Defects and errata are recorded
# ============================================================================================
@pytest.mark.requirement("F1")
@pytest.mark.live
@NOT_BUILT
def test_AT_F1():
    """AT-F1: SL-DEF-0001 holds the real cpu_ss_hsel facts; one erratum each for eth, compute
    and QuickStart; the eth forms agree with containment."""
    from nanosoc_rm.analysis.contains import contains
    from nanosoc_rm.registry import Repo, load_registry   # Repo: G14

    # Given: the live registry. When: validate and load.
    path = Path(require_env("NANOSOC_RM_REGISTRY"))
    cp = rm("registry", "validate", path, "--json")
    assert cp.returncode == 0, cp.stdout                  # Then 1
    reg = load_registry(path)

    # Then 2: the defect record holds the real facts
    d = {as_doc(x)["id"]: as_doc(x) for x in reg.defects()}["SL-DEF-0001"]
    assert d["component"] == "nanosoc_gen"
    assert {(o["component"], o["commit"]) for o in d["origin"]} == {
        ("nanosoc_arch_tech", ORIGIN_ARCH), ("nanosoc_gen", ORIGIN_GEN)}
    forms = {f["id"]: f for f in d["forms"]}
    assert sorted(forms) == ["A", "B"]
    assert [c["commit"] for c in forms["A"]["fixed_in"]] == [FIX_FORM_A]
    assert [c["commit"] for c in forms["B"]["fixed_in"]] == [FIX_FORM_B]
    assert d["trigger"].strip()
    det = {x["id"]: x for x in d["detectors"]}
    assert det["D1"]["kind"] == "generated-rtl"
    assert det["D1"]["source"] == {"component": "nanosoc_gen", "commit": FIX_FORM_B}
    assert det["D2"]["kind"] == "netlist" and det["D2"]["pattern"]
    assert sorted(e["id"] for e in d["effects"]) == ["E1", "E2", "E3", "E4", "E5"]
    # Pinned by commit. If the bench is re-committed before it is pushed, update this value.
    assert d["regression_test"] == {"component": "nanosoc-ethernet-chiplet",
                                    "commit": BENCH_UNPUSHED, "path": "verif/cpu_ss_hsel",
                                    "expect": {"shipped": "defect", "gated": "clean"}}

    # Then 3: one SL-DEF-0001 erratum per product, found through the product's real repo
    configs = {as_doc(c)["id"]: as_doc(c) for c in reg.configurations()}
    errata = [as_doc(e) for e in reg.errata() if as_doc(e)["defect"] == "SL-DEF-0001"]
    for host, repo_path in (ETH_REPO, COMPUTE_REPO, QS_REPO):
        comp, role = reg.resolve_repo(Repo(host=host, path=repo_path))
        assert comp is not None, "%s is not catalogued; F1 needs its erratum" % repo_path
        mine = [e for e in errata if configs[e["configuration"]]["component"] == comp]
        assert mine, "no SL-DEF-0001 erratum for any configuration of %s" % comp
        for e in mine:
            missing = [k for k in ERRATUM_F1_FIELDS if k not in e]
            assert not missing, (e["id"], missing)
    eth = next(e for e in errata if e["id"] == "ETH-R0P0-ERR-001")
    assert (eth["configuration"], eth["forms_present"], eth["status"]) == (
        "eth-rc4", ["A", "B"], "permanent")

    # Then 4: the recorded forms agree with containment: rc4 contains neither fix (B4 golden)
    for fix in (FIX_FORM_A, FIX_FORM_B):
        facts = [as_doc(o)["fact"] for o in contains(fix, component="nanosoc_gen",
                                                     configurations=["eth-rc4"])]
        gl = [f for f in facts if f["host"] == "gitlab"]
        assert [f["answer"] for f in gl] == ["no"], fix
        assert {p["commit"] for p in gl[0]["pins"]} == set(GEN_PINS_RC4)
        assert all(p["contains"] is False for p in gl[0]["pins"])


@pytest.mark.requirement("F1")
def test_AT_F1_negative_control(tmp_path):
    """AT-F1 control: the rc4 erratum copied to non-silicon eth-main, still `permanent`, is
    refused; an erratum naming a form the defect lacks is refused."""
    from nanosoc_rm.registry import RegistryError, load_registry

    # Given: the example registry loads (the pair's good half)
    good = example_registry(tmp_path / "good")
    load_registry(good)

    # When: the rc4 erratum is copied to eth-main (not silicon) and keeps status: permanent
    bad = example_registry(tmp_path / "copied")
    err = load_yaml(EX / "erratum.ETH-R0P0-ERR-001.example.yaml")
    err["configuration"], err["id"] = "eth-main", "ETH-MAIN-ERR-001"
    dump_yaml(err, bad / "errata" / "eth-main" / "ERR-001.yaml")

    # Then: refused by README section 3 rule 6, at the status field of that file
    with pytest.raises(RegistryError) as ei:
        load_registry(bad)
    assert any(f.endswith("errata/eth-main/ERR-001.yaml") and ptr == "/status"
               for f, ptr, _rule, _msg in problems_of(ei)), problems_of(ei)

    # When: the rc4 erratum names form C, which SL-DEF-0001 does not define. Then: rule 4.
    formc = example_registry(tmp_path / "form-c")
    edit_yaml(formc / "errata" / "eth-rc4" / "ERR-001.yaml",
              lambda e: e.__setitem__("forms_present", ["A", "C"]))
    with pytest.raises(RegistryError) as ei:
        load_registry(formc)
    assert any(ptr == "/forms_present/1" for _f, ptr, _rule, _msg in problems_of(ei)), problems_of(ei)


# ============================================================================================
# AT-F2  Exposure says how it was proven
# ============================================================================================
EXPOSURE_COLOUR = {"probably-exposed": "state-amber", "exposed": "state-red",
                   "affected": "state-red", "not-affected": "state-green"}   # markup: G5


@pytest.mark.requirement("F2")
@pytest.mark.live
@NOT_BUILT
def test_AT_F2():
    """AT-F2: every erratum shows level, method and evidence; ancestry shows amber; a detector
    level carries a re-checkable run; D1 runs on rc4's archived RTL."""
    from nanosoc_rm.analysis import detect                # PROPOSED (G10)
    from nanosoc_rm.registry import load_registry

    # Given: the live registry and the rc4 P1 archive
    reg = load_registry(Path(require_env("NANOSOC_RM_REGISTRY")))
    defects = {as_doc(x)["id"]: as_doc(x) for x in reg.defects()}
    errata = [as_doc(e) for e in reg.errata()]
    assert errata, "no erratum: nothing to show"
    client = member_client(Path(require_env("NANOSOC_RM_CONFIG")))

    # When: each configuration's errata page is rendered. Then 1-3.
    for e in errata:
        exp = e["exposure"]
        assert exp["level"] and exp["method"] and exp["evidence"].strip(), e["id"]
        html = client.get("/errata/" + e["configuration"]).text
        (el,) = [a for a in elements_with(html, "data-erratum") if a["data-erratum"] == e["id"]]
        assert el["data-exposure"] == exp["level"]
        assert el["data-method"] == exp["method"]
        assert EXPOSURE_COLOUR[exp["level"]] in el.get("class", "").split()
        assert exp["evidence"].strip()[:40] in html_unescape(html)   # printed beside the level
        if exp["level"] != "probably-exposed":
            assert exp["method"] == "detector", e["id"]
            assert exp["detector"] in {x["id"] for x in defects[e["defect"]]["detectors"]}
            run = exp["run"]                               # PROPOSED structured run (G10)
            assert run["input"]["md5"] and run["detector_source"]["commit"], e["id"]

    # Then 4: the rc4 erratum today is ancestry-only, amber, and names the archive it did not run on
    eth = next(e for e in errata if e["id"] == "ETH-R0P0-ERR-001")
    if eth["exposure"]["method"] == "ancestry":
        assert eth["exposure"]["level"] == "probably-exposed"
        assert RC4_BUILD_SOC_MD5 in eth["exposure"]["evidence"]

    # Then 5: D1 (hsel_lint at nanosoc_gen adbb9ab) runs on rc4's archived generated RTL
    run = detect.run("SL-DEF-0001", "D1", Path(require_env("RM_RC4_BUILD_SOC_TAR")))
    assert run.input["md5"] == RC4_BUILD_SOC_MD5                  # CR-3: DetectorRun.input is a dict
    assert run.detector_source == {"component": "nanosoc_gen", "commit": FIX_FORM_B}
    assert run.result in ("fired", "clean"), run.result            # never an error
    if run.result == "fired":
        assert set(run.forms) <= {"A", "B"}
    if eth["exposure"]["method"] == "detector":                   # once recorded, it must agree
        agree = {"fired": ("exposed", "affected"), "clean": ("not-affected",)}[run.result]
        assert eth["exposure"]["level"] in agree


@pytest.mark.requirement("F2")
def test_AT_F2_negative_control(tmp_path, monkeypatch):
    """AT-F2 control: ancestry claiming `exposed` is rejected; a detector the defect lacks is
    refused; a detector is shown both to fire and to stay clean (coverage review Medium 2)."""
    from nanosoc_rm.analysis import detect                # interfaces.md section 6a
    from nanosoc_rm.registry import RegistryError, load_registry
    load_registry(example_registry(tmp_path / "good"))     # the pair's good half

    # When: the real invalid example replaces the rc4 erratum. Then: rejected by the schema rule.
    anc = example_registry(tmp_path / "ancestry")
    shutil.copyfile(BAD / "erratum.ancestry-claims-exposed.yaml",
                    anc / "errata" / "eth-rc4" / "ERR-001.yaml")
    with pytest.raises(RegistryError) as ei:
        load_registry(anc)
    # matched on the pointer and the property name, not on jsonschema's message wording
    assert any(ptr.startswith("/exposure") and "detector" in msg
               for _f, ptr, _rule, msg in problems_of(ei)), problems_of(ei)

    # When: exposure claims detector D9, which SL-DEF-0001 lacks. Then: README section 3 rule 4.
    d9 = example_registry(tmp_path / "d9")
    edit_yaml(d9 / "errata" / "eth-rc4" / "ERR-001.yaml",
              lambda e: e.__setitem__("exposure", {
                  "level": "exposed", "method": "detector", "detector": "D9",
                  "evidence": "D9 fired on build_soc md5 %s" % RC4_BUILD_SOC_MD5}))
    with pytest.raises(RegistryError) as ei:
        load_registry(d9)
    assert any(ptr == "/exposure/detector" for _f, ptr, _rule, _msg in problems_of(ei)), problems_of(ei)

    # The detector path itself can say both answers: SL-DEF-0001's D2 (a netlist pattern) fires on a
    # synthetic netlist carrying the defect's shape and stays clean on one without it. A detector
    # that can only ever answer one way would pass AT-F2's "fired or clean" and prove nothing.
    write_config(tmp_path, monkeypatch)
    example_registry(tmp_path / "registry")
    fired, clean = synthetic_netlists(tmp_path / "netlists")
    hit = detect.run("SL-DEF-0001", "D2", fired)
    miss = detect.run("SL-DEF-0001", "D2", clean)
    assert (hit.result, hit.forms) == ("fired", ["A"]), hit
    assert (miss.result, miss.forms) == ("clean", []), miss
    assert hit.input["md5"] != miss.input["md5"]


# ============================================================================================
# AT-F3  Every cited commit exists on its canonical host
# ============================================================================================
# Every commit R0 cites, each pushed and reachable from a branch or tag on its canonical host. The last
# four are named in the erratum's free text and cited structurally in its `cites` (loader rule 9), so
# the gate proves them too (coverage review Low 2).
R0_REFS = {
    ("nanosoc_arch_tech", ORIGIN_ARCH),                 # GitHub main
    ("nanosoc_gen", ORIGIN_GEN),                        # GitLab main
    ("nanosoc_gen", FIX_FORM_A),                        # GitLab main
    ("nanosoc_gen", FIX_FORM_B),                        # GitLab main; also D1's source
    ("nanosoc-multicore-system", WORKAROUND_BRANCH_ONLY),  # GitLab, branch-only
    ("nanosoc-ethernet-chiplet", IMPORT_DOCS_COMMIT),   # GitHub, reachable only from tag v1.0.0
    ("nanosoc_gen", GEN_PINS_RC4[0]),                   # d04de2e: GitLab, keep tag and a branch
    ("nanosoc_gen", GEN_PINS_RC4[1]),                   # 1d7772d: GitLab main
    ("nanosoc-multicore-system", MULTICORE_DMA250),     # 57d2c76: GitLab master
    ("nanosoc-multicore-system", MULTICORE_RC4_PIN),    # f60449b: GitLab, keep tag and branches
}


def _registry_r0(root):
    """R0: the real records, with the unpushed bench taken out (regression_test: null)."""
    r0 = example_registry(root)
    edit_yaml(r0 / "defects" / "SL-DEF-0001.yaml",
              lambda d: d.__setitem__("regression_test", None))
    return r0


def _refs_gate(tmp_path, registry_dir, name):
    """t0.registry.refs-resolve with its control, anonymous, as the job runs it."""
    from nanosoc_rm.t0 import gates
    ctx = gates_ctx(nonce="0" * 32, registry_dir=registry_dir, workdir=tmp_path / ("work-" + name))
    row, ctl = gates.run_with_control("t0.registry.refs-resolve", ctx)
    return row, ctl, ctx.items("t0.registry.refs-resolve")    # items/<gate>.jsonl: PROPOSED (G11)


@pytest.mark.requirement("F3")
@pytest.mark.live
def test_AT_F3(tmp_path):
    """AT-F3: with every cited commit pushed, t0.registry.refs-resolve passes and names each
    commit it proved on its canonical host."""
    from nanosoc_rm.registry import load_registry

    # Given: R0
    r0 = _registry_r0(tmp_path / "r0")
    canonical = {as_doc(c)["id"]: as_doc(c)["canonical"]["host"]
                 for c in load_registry(r0).components()}

    # When: the gate runs anonymously
    row, ctl, items = _refs_gate(tmp_path, r0, "r0")

    # Then: PASS, control fired, every cited commit proved on its canonical host
    assert row["status"] == "PASS", row["detail"]
    assert ctl["status"] == "PASS"
    assert {(i["component"], i["commit"]) for i in items} == R0_REFS
    for i in items:
        assert i["result"] == "pass", i
        assert i["host"] == canonical[i["component"]], i


@pytest.mark.requirement("F3")
@pytest.mark.live
def test_AT_F3_negative_control(tmp_path):
    """AT-F3 control (the done-when, real data): the real SL-DEF-0001, whose bench 6aafc94 is
    unpushed, fails T0; so do a copy-host-only commit and a local-only commit."""
    # Case 1: R0 with the bench restored, which is the real record today
    r1 = _registry_r0(tmp_path / "r1")
    shutil.copyfile(EX / "defect.SL-DEF-0001.example.yaml", r1 / "defects" / "SL-DEF-0001.yaml")
    assert not anon_fetchable(tmp_path / "oracle", ETH_HTTPS, BENCH_UNPUSHED), \
        "6aafc94 is now pushed: this real-data control has expired; plant another unpushed commit"
    row, ctl, items = _refs_gate(tmp_path, r1, "r1")
    assert row["status"] == "FAIL" and ctl["status"] == "PASS"
    failed = [i for i in items if i["result"] == "fail"]
    assert [(i["file"], i["pointer"], i["commit"]) for i in failed] == [
        ("defects/SL-DEF-0001.yaml", "/regression_test/commit", BENCH_UNPUSHED)]
    assert BENCH_UNPUSHED[:7] in row["detail"]

    # Case 2: a record citing an ahb_qspi commit that exists only on the NON-canonical host.
    # ahb_qspi is DIVERGED (GitHub +1, GitLab +5), so such a commit exists whichever host
    # question 2 makes canonical: 1e5117e is GitLab-only, 71f8d1c is GitHub-only (CR-2: e908ac8 is no longer
    # GitLab-only since the 2026-10-01 keep tag on GitHub).
    from nanosoc_rm.registry import load_registry
    r2 = _registry_r0(tmp_path / "r2")
    canon = {as_doc(c)["id"]: as_doc(c)["canonical"]["host"]
             for c in load_registry(r2).components()}["ahb_qspi"]
    copy_only = AHB_QSPI_GITLAB_ONLY_NOW if canon == "github" else AHB_QSPI_GITHUB_ONLY
    d2 = load_yaml(EX / "defect.SL-DEF-0001.example.yaml")
    d2.update(id="SL-DEF-0002", component="ahb_qspi", regression_test=None,
              origin=[{"component": "ahb_qspi", "commit": copy_only}])
    dump_yaml(d2, r2 / "defects" / "SL-DEF-0002.yaml")
    row, _ctl, items = _refs_gate(tmp_path, r2, "r2")
    assert row["status"] == "FAIL"
    (q,) = [i for i in items if i["commit"] == copy_only]
    assert (q["result"], q["host"]) == ("fail", canon)         # judged on the canonical host

    # Case 3: an import source at the QuickStart write-up, committed only locally.
    # SYNTHETIC catalogue entry: the QuickStart component is not catalogued yet (G9).
    r3 = _registry_r0(tmp_path / "r3")
    edit_yaml(r3 / "catalogue.yaml", lambda c: c["components"].append(
        {"id": "nanosoc-m0-quickstart", "class": "example",
         "canonical": {"host": QS_REPO[0], "path": QS_REPO[1]}}))
    edit_yaml(r3 / "imports.yaml", lambda l: l["sources"].append(
        {"doc": {"component": "nanosoc-m0-quickstart", "commit": QS_WRITEUP_LOCAL_ONLY,
                 "path": "docs/DEFECT-cpu-ss-hsel.md"},
         "items": [{"key": "cpu_ss_hsel", "title": "CPU subsystem ignores cpu_ss_hsel",
                    "disposition": "record", "record": "SL-DEF-0001"}]}))
    row, _ctl, items = _refs_gate(tmp_path, r3, "r3")
    assert row["status"] == "FAIL"
    assert [i["commit"] for i in items if i["result"] == "fail"] == [QS_WRITEUP_LOCAL_ONLY]


# ============================================================================================
# AT-F5  Existing registers are imported
# ============================================================================================
@pytest.mark.requirement("F5")
@pytest.mark.live
@NOT_BUILT
def test_AT_F5(tmp_path):
    """AT-F5: all four registers are imported; each document's items are censused against the
    ledger; nothing is pending; every item is a record or a reasoned dismissal."""
    from nanosoc_rm.registry import Repo, load_registry

    # Given: the live registry's import ledger
    path = Path(require_env("NANOSOC_RM_REGISTRY"))
    reg = load_registry(path)
    eth, _ = reg.resolve_repo(Repo(host=ETH_REPO[0], path=ETH_REPO[1]))
    qs, _ = reg.resolve_repo(Repo(host=QS_REPO[0], path=QS_REPO[1]))
    assert eth and qs, "eth or QuickStart is not catalogued"
    ledger = as_doc(reg.imports())
    defect_ids = {as_doc(x)["id"] for x in reg.defects()}

    # Then 1: exactly the four sources F5 names
    assert sorted((s["doc"]["component"], s["doc"]["path"]) for s in ledger["sources"]) == sorted([
        (eth, "docs/tapeout/11-known-issues.md"), (eth, "docs/tapeout/16-open-defects.md"),
        (eth, "docs/tapeout/67-rc4-issue-register.md"), (eth, "docs/TAPEOUT_LIMITATION_CPU_SS_HSEL.md")])  # CR-13

    # When: the census runs. PROPOSED (G12): `nanosoc-rm registry imports`.
    cp = rm("registry", "imports", path, "--json")
    report = {r["path"]: r for r in (json.loads(x) for x in cp.stdout.splitlines() if x.strip())}

    # Then 2-3: every source's items match the ledger; nothing pending; exit 0. The report covers
    # exactly the four sources (a report that dropped one would otherwise pass).
    assert cp.returncode == 0, cp.stdout
    assert sorted(report) == sorted(s["doc"]["path"] for s in ledger["sources"])
    for p, r in report.items():
        assert (r["missing"], r["extra"], r["pending"]) == ([], [], 0), p
        assert r["census"], "%s: the rule extracted no items" % p

    # Independent census of the two eth documents, at whatever commit the ledger cites
    known = report["docs/tapeout/11-known-issues.md"]
    text = anon_show(tmp_path / "eth-11", ETH_HTTPS, known["commit"], "docs/tapeout/11-known-issues.md")
    assert text is not None, "11-known-issues.md is not fetchable at the cited commit %s" % known["commit"][:7]
    own = tuple(line[3:5] for line in text.splitlines()
                if len(line) > 5 and line.startswith("## ") and line[4] == ")")
    assert set(own) <= set(known["census"]) | {x["key"] for x in known["excluded"]}
    if known["commit"] == IMPORT_DOCS_COMMIT:
        assert own == KNOWN_ISSUES_KEYS
    opened = report["docs/tapeout/16-open-defects.md"]
    text = anon_show(tmp_path / "eth-16", ETH_HTTPS, opened["commit"], "docs/tapeout/16-open-defects.md")
    assert text is not None, "16-open-defects.md is not fetchable at the cited commit %s" % opened["commit"][:7]
    numbered = {line[3:].split()[0] for line in text.splitlines()
                if line.startswith("## ") and line[3:4].isdigit()}
    assert numbered <= set(opened["census"]) | {x["key"] for x in opened["excluded"]}
    assert all(x["reason"].strip() for x in opened["excluded"])
    if opened["commit"] == IMPORT_DOCS_COMMIT:
        assert set(OPEN_DEFECTS_NUMBERED) == numbered
        # 6 numbered items + 2 "found in passing" paragraphs + the 2026-08-12 addendum's
        # "found in passing" defect: the rule must import (include) or explicitly exclude every one.
        assert len(opened["census"]) + len(opened["excluded"]) >= OPEN_DEFECTS_CANDIDATES

    # Then 4-5: every item is a record of an existing defect, or dismissed with a reason
    for s in ledger["sources"]:
        for it in s["items"]:
            assert it["disposition"] in ("record", "dismissed"), (s["doc"]["path"], it["key"])
            if it["disposition"] == "record":
                assert it["record"] in defect_ids, it
            else:
                assert it["reason"].strip(), it
    qs_items = next(s for s in ledger["sources"] if s["doc"]["component"] == qs)["items"]
    assert any(it.get("record") == "SL-DEF-0001" for it in qs_items)


@pytest.mark.requirement("F5")
def test_AT_F5_negative_control(tmp_path):
    """AT-F5 control: the example ledger (all pending) is reported not done; a ledger missing
    one censused item is reported not done, naming the item."""
    from nanosoc_rm.registry import load_registry
    from nanosoc_rm.registry.imports import census        # PROPOSED (G12)

    # SYNTHETIC documents with exactly the items the example ledger lists, and a third source.
    docs = {
        "docs/tapeout/11-known-issues.md": "# 11\n## a) first\n## b) second\n",
        "docs/tapeout/16-open-defects.md": "# 16\n## 1 — first\n",
        "docs/synthetic-register.md": "# R\n## a) one\n## b) two\n## c) three\n",
    }

    def read_doc(component, commit, path):
        return docs[path]

    # Each source declares how its items are found (PROPOSED items_rule, G12).
    rule = {"pattern": r"^## ([a-z]\)|[0-9]+) "}

    def add_rules(ledger):
        for s in ledger["sources"]:
            s["doc"]["items_rule"] = dict(rule)

    # Given / When: the example ledger as written. Then: census matches, but 3 pending: not done.
    pending_dir = example_registry(tmp_path / "pending")
    edit_yaml(pending_dir / "imports.yaml", add_rules)
    reg = load_registry(pending_dir)
    rows = census(as_doc(reg.imports()), read_doc=read_doc)
    assert sum(r.pending for r in rows) == 3
    assert not all(r.done for r in rows)

    # Given: a source with a), b), c), and a ledger listing only a) and b), both disposed
    reg_dir = example_registry(tmp_path / "missing")

    def add_source(ledger):
        for s in ledger["sources"]:
            for it in s["items"]:
                it.update(disposition="dismissed", reason="synthetic: control only")
        add_rules(ledger)
        ledger["sources"].append({
            "doc": {"component": "nanosoc-ethernet-chiplet", "commit": IMPORT_DOCS_COMMIT,
                    "path": "docs/synthetic-register.md", "items_rule": dict(rule)},
            "items": [{"key": "a)", "title": "one", "disposition": "dismissed", "reason": "control"},
                      {"key": "b)", "title": "two", "disposition": "dismissed", "reason": "control"}]})

    edit_yaml(reg_dir / "imports.yaml", add_source)

    # When: the census runs. Then: c) is reported missing, and the import is not done.
    rows = {r.path: r for r in census(as_doc(load_registry(reg_dir).imports()), read_doc=read_doc)}
    syn = rows["docs/synthetic-register.md"]
    assert syn.missing == ["c)"] and syn.pending == 0
    assert not syn.done


# ============================================================================================
# Self-checks: they test THIS FILE, not the tool, and run now (PR6 at checker level; coverage
# review High 3). Each proves a negative control's input really is what its AT claims, using the
# reference oracles above or the contracts' own schemas, never tool code.
# ============================================================================================
def _schemas():
    ce = _check_examples()
    schemas, registry = ce.load_schemas()
    return ce, schemas, registry


def _errors(schema, doc, pointer=None):
    ce, schemas, registry = _schemas()
    return list(ce.validator_for(schemas, registry, schema, pointer).iter_errors(doc))


def test_selfcheck_D1_budget_inputs():
    """AT-D1: the example bundle ran 92 s (inside 120 s); the control's 181 s is over; seven tiers."""
    prov = example_provenance()
    seconds = lambda a, b: int((utc(b) - utc(a)).total_seconds())
    assert seconds(prov["started_at"], prov["finished_at"]) == 92
    assert seconds(prov["started_at"], "2026-10-01T02:06:31Z") == 181 > 120
    tiers = json.loads((SCHEMAS / "result-bundle.schema.json").read_text())["properties"]["tier"]["enum"]
    assert tiers == ["t0", "t1", "t2", "t3", "t4", "t5", "t6"]


def test_selfcheck_D2_expected_sets():
    """AT-D2: the per-pin expectations agree with the contract examples they are drawn from."""
    lock = load_yaml(EX / "release-lock.eth-rc4.example.yaml")["pins"]
    assert len(lock) == RC4_PIN_COUNT
    assert {p["tree_paths"][0] for p in lock if p["url_kind"] == "ssh"} == set(RC4_SSH_PINS)
    assert sum(1 for p in lock if p["tree_paths"][0].startswith("tidelink/")) == RC4_BELOW_TIDELINK
    rows = [json.loads(line) for line in (EX / "observation.example.jsonl").read_text().splitlines() if line]
    silicon = [r["fact"] for r in rows if r["kind"] == "silicon_protection"]
    assert {r["tree_paths"][0] for r in silicon if r["pinned"]} == set(RC4_KEEP_TAG_PINS)
    catalogue = {c["id"]: c for c in load_yaml(EX / "component-catalogue.example.yaml")["components"]}
    assert catalogue["ahb_qspi"]["canonical"]["host"] == "github"      # e908ac8 is judged on GitHub...
    golden = {r["gate"]: r for r in parse_rows(example_verdicts())}
    # ...where it is tagged since 2026-10-01 (CR-2): only the private toolkit leaves the gate UNVERIFIED
    assert golden["t0.pins.reachable"]["status"] == "UNVERIFIED" and "e908ac8" not in golden["t0.pins.reachable"]["detail"]
    for p in RC4_SSH_PINS:
        assert p in golden["t0.urls.no-ssh-public"]["detail"] and p in golden["t0.pins.fetchable"]["detail"]


def test_selfcheck_D2_gate_fixtures(tmp_path):
    """AT-D2 control part 2: the hermetic good and broken inputs really are good and broken, shown with
    raw git and the schemas (no tool code). The Arm specimen is a stand-in here; the AT uses the tool's."""
    stand_in = "STAND-IN FOR THE TOOL'S arm.banner SPECIMEN"
    fx = hermetic_gate_fixtures(tmp_path / "fx", arm_specimen=stand_in)
    c, opts = fx["commits"], fx_insteadof(fx["url_map"])

    def upgrade(name, old, new):
        work = tmp_path / ("clone-" + name)
        _fx_git(*opts, "clone", "-q", FX_WEB % name, work)
        _fx_git(*opts, "-C", work, "checkout", "-q", old)
        _fx_git(*opts, "-C", work, "submodule", "update", "--init", "--recursive")
        _fx_git(*opts, "-C", work, "checkout", "-q", new)
        first = _fx_git(*opts, "-C", work, "submodule", "update", "--init", "--recursive", check=False)
        return first.returncode

    assert upgrade("fx-upgrade-good", c["ug1"], c["ug2"]) == 0, "good pair: the old host still serves s3"
    assert upgrade("fx-upgrade-bad", c["ub1"], c["ub2"]) != 0, "bad pair: an upgrade without sync must fail"
    import yaml
    show = lambda name, commit, path: _fx_git("--git-dir", fx["url_map"][FX_WEB % name], "show",
                                             "%s:%s" % (commit, path)).stdout
    assert _errors("component-manifest", yaml.safe_load(show("fx-manifest-good", c["mg"], "nanosoc-component.yaml"))) == []
    assert [e.validator for e in _errors("component-manifest",
                                         yaml.safe_load(show("fx-manifest-bad", c["mb"], "nanosoc-component.yaml")))] == ["pattern"]
    grep = lambda name, commit: _fx_git("--git-dir", fx["url_map"][FX_WEB % name], "grep", "-l", stand_in, commit,
                                       check=False).returncode
    assert grep("fx-vendor-planted", c["vp"]) == 0 and grep("fx-vendor-clean", c["vc"]) != 0
    good_reg, bad_reg = fx["registries"]
    assert _errors("component-catalogue", load_yaml(good_reg / "catalogue.yaml")) == []
    assert _errors("component-catalogue", load_yaml(bad_reg / "catalogue.yaml"))
    assert [g for g, _good, _bad in fx["cases"]] == ["t0.manifest.valid", "t0.clone.upgrade-path",
                                                    "t0.vendor.no-arm-ip", "t0.registry.schema"]


def test_selfcheck_D5_tampers():
    """AT-D5: the genuine case passes every verify check; each tamper fails exactly the check it is
    listed under; the control (captured commit = rc4's parent) fails check 5."""
    ce = _check_examples()
    genuine = genuine_case()
    assert reference_verify(genuine, ce) is None
    assert _errors("result-bundle", json.loads(genuine.run["request"]), "/$defs/rm_request") == []
    for label, check, mutate in D5_TAMPERS:
        case = genuine_case()
        mutate(case)
        assert dataclasses.asdict(case) != dataclasses.asdict(genuine_case()), label
        assert reference_verify(case, ce) == check, (label, reference_verify(case, ce), check)
    control = genuine_case()
    control.prov["captured"]["superproject_commit"] = RC4_PARENT
    assert reference_verify(control, ce) == 5
    changed = [k for k in ("run", "pipeline", "job", "variables", "verdicts")
               if getattr(control, k) != getattr(genuine_case(), k)]
    assert changed == [], "the control must differ from the genuine case in provenance only"


def test_selfcheck_D6_rollup_oracle():
    """AT-D6: README section 6 applied by a reference oracle gives EXPECTED_RC4_ROLLUP on the golden
    rows; each control variant is UNVERIFIED; nothing-checked runs are never PASS, by the oracle and
    by the observation schema."""
    rows = parse_rows(example_verdicts())
    items = reference_rollup(rows, CONFIG_GATES)
    assert items == EXPECTED_RC4_ROLLUP
    assert reference_run_verdict(items) == ("FAIL", 2)
    no_ctl = [r for r in rows if r["gate"] != "t0.infra.probe.negctl"]
    assert reference_rollup(no_ctl, CONFIG_GATES)["t0.infra.probe"] == ("UNVERIFIED", "missing")
    unfired = copy.deepcopy(rows)
    next(r for r in unfired if r["gate"] == "t0.infra.probe.negctl")["status"] = "FAIL"
    assert reference_rollup(unfired, CONFIG_GATES)["t0.infra.probe"] == ("UNVERIFIED", "not-fired")
    twice = rows + [dict(next(r for r in rows if r["gate"] == "t0.infra.probe"))]
    assert reference_rollup(twice, CONFIG_GATES)["t0.infra.probe"][0] == "UNVERIFIED"
    skipped = [dict(r, status="SKIP") for r in rows if not r["gate"].endswith(".negctl")]
    assert reference_run_verdict(reference_rollup(skipped, CONFIG_GATES))[0] == "UNVERIFIED"
    assert reference_run_verdict({})[0] == "UNVERIFIED"
    # The schema agrees: the golden roll-up stored as an accepted FAIL is valid; the same gates
    # claimed as PASS are refused; a PASS over nothing but NOT-RUN gates is refused.
    base = json.loads([line for line in (EX / "observation.example.jsonl").read_text().splitlines()
                       if '"kind": "t0_result"' in line][0])
    f = base["fact"]
    f.update(acceptance="accepted", reason=None, verdict="FAIL", bundle={"store": "ci", "path": "rm-bundle"},
             started_at="2026-10-01T02:03:30Z", finished_at="2026-10-01T02:05:02Z", over_budget=False,
             runner={"id": RUNNER_ID, "description": "rm-hardened", "tags": ["rm-hardened"]},
             gates=[{"id": g, "verdict": v, "control": c, "detail": "x"} for g, (v, c) in items.items()])
    assert _errors("observation", base) == []
    f["verdict"] = "PASS"
    assert _errors("observation", base)
    f["gates"] = [g for g in f["gates"] if g["verdict"] == "NOT-RUN"]
    assert _errors("observation", base)


def test_selfcheck_D6_waiver_oracle():
    """GATE2 Q1: rc4's 7 waivers (8 until CR-2) cover every non-pass item of its four non-pass gates, so the oracle
    rolls rc4 up WAIVED; an uncovered failure, or a control that did not fire, stays red; a waiver whose
    cause has gone is unused. The stored example agrees, and the schema refuses a WAIVED run that still
    has a FAIL gate."""
    raw = reference_rollup(parse_rows(example_verdicts()), CONFIG_GATES)
    items, waivers = example_items(), example_waivers()
    assert sorted(items) == sorted(RC4_WAIVED_GATES) and len(waivers) == 7
    waived, unused = reference_apply_waivers(raw, items, waivers)
    assert waived == EXPECTED_RC4_WAIVED_ROLLUP and unused == []
    assert reference_run_verdict(waived) == ("WAIVED", 2)
    assert reference_run_verdict(raw) == ("FAIL", 2)
    # The stored example is the same roll-up, item for item
    ex = json.loads((EX / "observation.t0-result-waived.example.jsonl").read_text())
    assert _errors("observation", ex) == []
    f = ex["fact"]
    assert {g["id"]: (g["verdict"], g["control"]) for g in f["gates"]} == EXPECTED_RC4_WAIVED_ROLLUP
    assert f["verdict"] == "WAIVED"
    for g in f["gates"]:
        if g["verdict"] == "WAIVED":
            assert g["raw_verdict"] == raw[g["id"]][0]
            assert sorted(x["tree_path"] for x in g["waived"]) == sorted(
                i["tree_path"] for i in items[g["id"]] if i["result"] != "pass")
    # A new failure stays red: one uncovered item keeps its gate FAIL, and the run FAIL
    new = copy.deepcopy(items)
    new["t0.pins.reachable"].append(dict(NEW_FAILURE))
    # CR-2: the job that saw it writes FAIL for the gate (today's raw row is UNVERIFIED: the toolkit)
    w, _ = reference_apply_waivers(dict(raw, **{"t0.pins.reachable": ("FAIL", "fired")}), new, waivers)
    assert w["t0.pins.reachable"] == ("FAIL", "fired") and reference_run_verdict(w)[0] == "FAIL"
    # A gate whose control did not fire is never waived (D6)
    w, _ = reference_apply_waivers(dict(raw, **{"t0.pins.fetchable": ("FAIL", "not-fired")}), items, waivers)
    assert w["t0.pins.fetchable"] == ("FAIL", "not-fired")
    # Only a not-reached item rides on the waiver above it: a FAIL below a waived pin is a new failure
    below = copy.deepcopy(items)
    next(i for i in below["t0.pins.fetchable"] if i["tree_path"] == "tidelink/deps/tidelink-phy").update(
        result="fail", why="SYNTHETIC: a failure of its own, not caused by tidelink")
    w, _ = reference_apply_waivers(raw, below, waivers)
    assert w["t0.pins.fetchable"] == ("FAIL", "fired")
    # A not-reached item is covered only through a covered item above it
    orphan = copy.deepcopy(items)
    orphan["t0.pins.fetchable"] = [i for i in orphan["t0.pins.fetchable"] if i["tree_path"] != "tidelink"]
    w, _ = reference_apply_waivers(raw, orphan, [x for x in waivers if x["tree_path"] != "tidelink"])
    assert w["t0.pins.fetchable"] == ("FAIL", "fired")
    # A fixed cause retires its waiver: with e908ac8 on GitHub (GATE2 Q2) that item passes (CR-2: today's
    # items), so the waiver it had is unused; before, the same waiver covered it and the gate was FAIL raw
    w, unused = reference_apply_waivers(raw, items, waivers + [RETIRED_AHB_QSPI_WAIVER])
    assert w["t0.pins.reachable"] == ("WAIVED", "fired")
    assert [(u["gate"], u["tree_path"]) for u in unused] == [("t0.pins.reachable", AHB_QSPI_TREE_PATH)]
    before = copy.deepcopy(items)
    before["t0.pins.reachable"].append(dict(AHB_QSPI_UNREACHABLE_ITEM))
    w, unused = reference_apply_waivers(dict(raw, **{"t0.pins.reachable": ("FAIL", "fired")}), before,
                                        waivers + [RETIRED_AHB_QSPI_WAIVER])
    assert w["t0.pins.reachable"] == ("WAIVED", "fired") and unused == []
    w, _ = reference_apply_waivers(dict(raw, **{"t0.pins.reachable": ("FAIL", "fired")}), before, waivers)
    assert w["t0.pins.reachable"] == ("FAIL", "fired")          # without it, that item is a failure
    # Waivers without items change nothing; PASS and NOT-RUN gates are never touched
    assert reference_apply_waivers(raw, {}, waivers)[0] == raw
    # The schema agrees: a WAIVED run with a FAIL gate is refused; so is a WAIVED gate without raw_verdict
    hidden = copy.deepcopy(ex)
    g3 = hidden["fact"]["gates"][3]
    g3.pop("raw_verdict"), g3.pop("waived")
    g3["verdict"] = "FAIL"
    assert _errors("observation", hidden)
    bare = copy.deepcopy(ex)
    bare["fact"]["gates"][2].pop("raw_verdict")
    assert _errors("observation", bare)


def test_selfcheck_D10_environments(tmp_path):
    """AT-D10 control: each synthetic job environment breaks exactly one hardened property."""
    assert reference_hardened(job_env(tmp_path, "good"), SERVICE_ACCOUNT) == []
    for want, env in (("CI_COMMIT_REF_PROTECTED", job_env(tmp_path, "ref", {"CI_COMMIT_REF_PROTECTED": "false"})),
                      ("HOME", job_env(tmp_path, "home", home_file=True)),
                      ("TMPDIR", job_env(tmp_path, "tmpdir", tmp_file=True)),
                      ("USER", job_env(tmp_path, "user", {"USER": "jbloggs"})),
                      ("CI_JOB_TOKEN", job_env(tmp_path, "token", {"CI_JOB_TOKEN": "left-in-the-environment"}))):
        assert reference_hardened(env, SERVICE_ACCOUNT) == [want], want


def test_selfcheck_D11_planted_line(tmp_path):
    """AT-D11 control: a specimen appended to the two-line runner log starts at line 3, and the
    bundle writer puts it exactly there."""
    specimen = "STAND-IN SPECIMEN LINE 1\nSTAND-IN SPECIMEN LINE 2\n"
    b = write_bundle(tmp_path, example_provenance(), example_verdicts(), {"logs/t0.log": T0_LOG + specimen})
    lines = (b / "logs" / "t0.log").read_text().splitlines()
    assert lines.index("STAND-IN SPECIMEN LINE 1") + 1 == 3
    assert sorted(str(p.relative_to(b)) for p in b.rglob("*") if p.is_file()) == [
        "logs/t0.log", "provenance.json", "verdicts.tsv"]


def test_selfcheck_F1_F2_variants():
    """AT-F1 and AT-F2 controls: each broken record breaks exactly the rule the AT names."""
    elig = load_yaml(EX / "eligibility.example.yaml")
    silicon = {c["id"] for c in elig["configurations"] if "silicon" in c}
    defect = load_yaml(EX / "defect.SL-DEF-0001.example.yaml")
    forms, detectors = {f["id"] for f in defect["forms"]}, {d["id"] for d in defect["detectors"]}
    erratum = load_yaml(EX / "erratum.ETH-R0P0-ERR-001.example.yaml")
    assert _errors("erratum", erratum) == []
    assert erratum["status"] == "permanent" and erratum["configuration"] in silicon     # the good half
    copied = dict(erratum, configuration="eth-main", id="ETH-MAIN-ERR-001")
    assert copied["status"] == "permanent" and copied["configuration"] not in silicon   # rule 6 fires
    assert _errors("erratum", copied) == []                                             # only the loader sees it
    assert set(["A", "C"]) - forms == {"C"}                                             # rule 4 fires on form C
    ancestry = load_yaml(BAD / "erratum.ancestry-claims-exposed.yaml")
    assert any(e.validator == "required" and "detector" in e.message and list(e.absolute_path) == ["exposure"]
               for e in _errors("erratum", ancestry))
    assert "D9" not in detectors and detectors == {"D1", "D2"}                          # rule 4 fires on D9
    import re as _re
    fired, clean = synthetic_netlists_text()
    pattern = next(d for d in defect["detectors"] if d["id"] == "D2")["pattern"]
    assert _re.search(pattern, fired) and not _re.search(pattern, clean)


def synthetic_netlists_text():
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        fired, clean = synthetic_netlists(d)
        return fired.read_text(), clean.read_text()


def _structured_refs(registry_dir):
    """Every structured commit citation in a registry directory (what refs-resolve must prove)."""
    refs = set()
    for path in sorted(Path(registry_dir).rglob("*.yaml")):
        doc = load_yaml(path)
        if path.name == "imports.yaml":
            refs |= {(s["doc"]["component"], s["doc"]["commit"]) for s in doc["sources"]}
            continue
        if "origin" in doc:                                   # a defect
            refs |= {(r["component"], r["commit"]) for r in doc["origin"]}
            for f in doc["forms"]:
                refs |= {(r["component"], r["commit"]) for r in f.get("fixed_in", []) + f.get("candidate_fix", [])}
            refs |= {(d["source"]["component"], d["source"]["commit"]) for d in doc["detectors"] if "source" in d}
            if doc.get("regression_test"):
                refs.add((doc["regression_test"]["component"], doc["regression_test"]["commit"]))
        if "exposure" in doc:                                 # an erratum
            for impl in (doc.get("workaround") or {}).get("implementations", []):
                refs.add((impl["component"], impl["commit"]))
            run = doc["exposure"].get("run") or {}
            if run.get("detector_source"):
                refs.add((run["detector_source"]["component"], run["detector_source"]["commit"]))
        refs |= {(r["component"], r["commit"]) for r in doc.get("cites", [])}
    return refs


HEX_TOKEN = __import__("re").compile(r"(?<![0-9a-f])(?<!md5 )[0-9a-f]{7,40}(?![0-9a-f])")


def free_text_uncited(record):
    """Loader rule 9, as an oracle: hex tokens (7 to 40, with a digit, not after 'md5 ') in the
    record's free text that no structured citation of the record matches."""
    texts = [record.get("title", ""), record.get("trigger", ""), record.get("conditions", "")]
    texts += [e.get("text", "") for e in record.get("effects", [])]
    texts += [(record.get("exposure") or {}).get("evidence", ""), ((record.get("workaround") or {}).get("rules", ""))]
    cited = [c for _comp, c in _structured_refs_of(record)]
    tokens = [m.group(0) for t in texts for m in HEX_TOKEN.finditer(t or "") if any(ch.isdigit() for ch in m.group(0))]
    return sorted({tok for tok in tokens if not any(c.startswith(tok) for c in cited)})


def _structured_refs_of(record):
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        dump_yaml(record, Path(d) / "record.yaml")
        return _structured_refs(d)


def test_selfcheck_F3_refs(tmp_path):
    """AT-F3: R0's structured citations are exactly R0_REFS (10 commits); every commit the example
    records name in free text is cited structurally (rule 9); the control adds exactly the bench."""
    r0 = example_registry(tmp_path / "r0")
    edit_yaml(r0 / "defects" / "SL-DEF-0001.yaml", lambda d: d.__setitem__("regression_test", None))
    assert _structured_refs(r0) == R0_REFS
    r1 = example_registry(tmp_path / "r1")
    assert _structured_refs(r1) - R0_REFS == {("nanosoc-ethernet-chiplet", BENCH_UNPUSHED)}
    for name in ("defect.SL-DEF-0001.example.yaml", "erratum.ETH-R0P0-ERR-001.example.yaml"):
        assert free_text_uncited(load_yaml(EX / name)) == [], name
    sneaky = load_yaml(EX / "erratum.ETH-R0P0-ERR-001.example.yaml")
    sneaky["exposure"]["evidence"] += " Also seen at 7a8b9c0."
    assert free_text_uncited(sneaky) == ["7a8b9c0"]


def reference_census(source, text):
    import re as _re
    rule = source["doc"]["items_rule"]
    keys = [m.group(1) for m in _re.finditer(rule["pattern"], text, _re.M)]
    keys = [k for k in keys if k not in {x["key"] for x in rule.get("exclude", [])}]
    keys += [x["key"] for x in rule.get("include", [])]
    listed = [i["key"] for i in source["items"]]
    return {"missing": [k for k in keys if k not in listed], "extra": [k for k in listed if k not in keys],
            "pending": sum(1 for i in source["items"] if i["disposition"] == "pending")}


def test_selfcheck_F5_census():
    """AT-F5 control: on the synthetic documents, the example ledger is 3 pending, and a ledger that
    omits c) is missing exactly c)."""
    docs = {"docs/tapeout/11-known-issues.md": "# 11\n## a) first\n## b) second\n",
            "docs/tapeout/16-open-defects.md": "# 16\n## 1 — first\n",
            "docs/synthetic-register.md": "# R\n## a) one\n## b) two\n## c) three\n"}
    rule = {"pattern": r"^## ([a-z]\)|[0-9]+) "}
    ledger = load_yaml(EX / "import-ledger.example.yaml")
    for s in ledger["sources"]:
        s["doc"]["items_rule"] = dict(rule)
    assert _errors("import-ledger", ledger) == []
    rows = [reference_census(s, docs[s["doc"]["path"]]) for s in ledger["sources"]]
    assert sum(r["pending"] for r in rows) == 3 and all(r["missing"] == [] for r in rows)
    synthetic = {"doc": {"component": "nanosoc-ethernet-chiplet", "commit": IMPORT_DOCS_COMMIT,
                         "path": "docs/synthetic-register.md", "items_rule": dict(rule)},
                 "items": [{"key": "a)", "title": "one", "disposition": "dismissed", "reason": "control"},
                           {"key": "b)", "title": "two", "disposition": "dismissed", "reason": "control"}]}
    assert reference_census(synthetic, docs["docs/synthetic-register.md"]) == {"missing": ["c)"], "extra": [], "pending": 0}
