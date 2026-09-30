"""Gate-2 acceptance tests: CI, T0 and errata.

One acceptance test per v1 Must requirement D1, D2, D5, D6, D10, D11, F1, F2, F3 and F5,
each with its negative control (PR6). The Given/When/Then, fixtures, and the reason each test
cannot pass vacuously are in docs/acceptance/ci_errata.md. The AT id in each docstring points
there.

Every test is skipped ("not implemented: gate 2") until the module it exercises exists. The
assertions are written against the gate-2 contracts (docs/contracts/, schemas/). A name marked
PROPOSED (Gn) is not in the contracts yet; see "Contract gaps" in the doc.

Markers (tests/conftest.py must register them, gap G13):
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
from html import escape as html_escape
from html.parser import HTMLParser
from pathlib import Path

import pytest

GATE2 = "not implemented: gate 2"

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
AHB_QSPI_GITLAB_ONLY = "e908ac843845d5b293616265f93a802804555540"
GEN_PINS_RC4 = ("d04de2ec25d79c1fc3223f853ddb6725cbf2831c",  # builds rc4's RTL
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

# Control state for a NOT-RUN gate. PROPOSED (G6): the schema offers only fired/not-fired/missing.
NOT_RUN_CONTROL = "not-applicable"

# The golden rc4 roll-up (README section 6 applied to result-bundle.verdicts.example.tsv, with the
# two t0.runner.hardened rows the example lacks added; G6).
EXPECTED_RC4_ROLLUP = {
    "t0.infra.probe": ("PASS", "fired"),
    "t0.runner.hardened": ("PASS", "fired"),
    "t0.pins.fetchable": ("FAIL", "fired"),
    "t0.pins.reachable": ("FAIL", "fired"),
    "t0.urls.no-ssh-public": ("FAIL", "fired"),
    "t0.manifest.valid": ("NOT-RUN", NOT_RUN_CONTROL),
    "t0.clone.upgrade-path": ("NOT-RUN", NOT_RUN_CONTROL),
    "t0.vendor.no-arm-ip": ("UNVERIFIED", "fired"),
}

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


FIELDS = ("utc", "status", "gate", "detail", "nonce")


def parse_rows(tsv):
    return [dict(zip(FIELDS, line.split("\t"))) for line in tsv.splitlines() if line]


def format_rows(rows):
    return "".join("\t".join(r[k] for k in FIELDS) + "\n" for r in rows)


def with_runner_hardened(tsv, nonce):
    """The golden example has no t0.runner.hardened rows (G6). Add the two a real run writes."""
    rows = parse_rows(tsv)
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


# ============================================================================================
# AT-D1  Every tier has a command and a budget
# ============================================================================================
@pytest.mark.requirement("D1")
@pytest.mark.live
@pytest.mark.skip(reason=GATE2)
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
@pytest.mark.skip(reason=GATE2)
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
@pytest.mark.skip(reason=GATE2)
def test_AT_D2(tmp_path):
    """AT-D2: every configuration and @registry has tonight's accepted T0 report; rc4 goes red
    on exactly its 3 known real gates; tidelink is judged on its canonical host."""
    from nanosoc_rm.registry import load_registry
    from nanosoc_rm.t0 import gates

    # Given: the live registry and one completed night
    reg = load_registry(Path(require_env("NANOSOC_RM_REGISTRY")))
    expected = sorted([as_doc(c)["id"] for c in reg.configurations()] + ["@registry"])
    canonical = {as_doc(c)["id"]: as_doc(c)["canonical"]["host"] for c in reg.components()}

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
        assert all(g["control"] in ("fired", "not-fired", "missing", NOT_RUN_CONTROL)
                   for g in f["gates"])

    # Then 3: eth-rc4 matches the golden rows, for the real reasons
    (rc4,) = by_cfg["eth-rc4"]
    assert rc4["commit"] == RC4
    g = {x["id"]: x for x in rc4["gates"]}
    assert g["t0.infra.probe"]["verdict"] == "PASS"
    assert g["t0.pins.fetchable"]["verdict"] == "FAIL"
    assert g["t0.pins.reachable"]["verdict"] == "FAIL"
    assert g["t0.urls.no-ssh-public"]["verdict"] == "FAIL"
    assert g["t0.manifest.valid"]["verdict"] == "NOT-RUN"
    assert g["t0.clone.upgrade-path"]["verdict"] == "NOT-RUN"      # eth-rc4 has no upgrade_from
    assert g["t0.vendor.no-arm-ip"]["verdict"] == "UNVERIFIED"     # 7 trees never fetched
    assert rc4["verdict"] == "FAIL"
    nonce = rc4["run"]["nonce"]

    fetch = gate_items(nonce, "t0.pins.fetchable")
    by_result = {}
    for i in fetch:
        by_result.setdefault(i["result"], set()).add(i["tree_path"])
    assert by_result.get("fail") == set(RC4_SSH_PINS)
    assert len(by_result.get("not-reached", ())) == RC4_BELOW_TIDELINK
    assert all(p.startswith("tidelink/") for p in by_result["not-reached"])
    assert len(by_result.get("pass", ())) == RC4_PIN_COUNT - len(RC4_SSH_PINS) - RC4_BELOW_TIDELINK

    reach = {i["tree_path"]: i for i in gate_items(nonce, "t0.pins.reachable")}
    # TideLink host drift: 5e8bdb5 is v0.90 on GitHub (canonical) and missing on the GitLab copy.
    assert reach["tidelink"]["commit"] == TIDELINK_PIN
    assert (reach["tidelink"]["result"], reach["tidelink"]["host"]) == ("pass", "github")
    assert reach["ASIC/asic-toolkit"]["result"] == "unverified"    # canonical repo is private
    qspi = reach["nanosoc-multicore-system/ahb_qspi"]
    assert qspi["commit"] == AHB_QSPI_GITLAB_ONLY
    assert qspi["result"] == ("fail" if canonical["ahb_qspi"] == "github" else "pass")

    ssh = gate_items(nonce, "t0.urls.no-ssh-public")
    assert {i["tree_path"] for i in ssh if i["result"] == "fail"} == set(RC4_SSH_PINS)

    # Then 4: the registry job judges every cited commit; the unpushed bench fails while unpushed
    (regrun,) = by_cfg["@registry"]
    refs = gate_items(regrun["run"]["nonce"], "t0.registry.refs-resolve")
    bench = [i for i in refs if i["commit"] == BENCH_UNPUSHED]
    if bench:
        pushed = anon_fetchable(tmp_path / "oracle", ETH_HTTPS, BENCH_UNPUSHED)
        assert bench[0]["result"] == ("pass" if pushed else "fail")
    rg = {x["id"]: x for x in regrun["gates"]}
    any_fail = any(i["result"] == "fail" for i in refs)
    assert rg["t0.registry.refs-resolve"]["verdict"] == ("FAIL" if any_fail else "PASS")

    # Then 5: the dashboard shows every configuration with tonight's verdict, data age < 24 h
    html = member_client(Path(require_env("NANOSOC_RM_CONFIG"))).get("/").text
    rows = {a["data-configuration"]: a for a in elements_with(html, "data-configuration")}
    assert sorted(rows) == sorted(c for c in expected if c != "@registry")
    for cfg, a in rows.items():
        assert a["data-verdict"] == by_cfg[cfg][0]["verdict"]
        assert int(a["data-age-seconds"]) < 24 * 3600


@pytest.mark.requirement("D2")
@pytest.mark.skip(reason=GATE2)
def test_AT_D2_negative_control(tmp_path, monkeypatch):
    """AT-D2 control: the CI API refuses eth-main's nightly trigger. eth-main must show a
    missed night, never last night's result and never PASS."""
    from nanosoc_rm import db
    from nanosoc_rm.ci.client import FakeCI              # PROPOSED (G4)
    from nanosoc_rm.ci.trigger import trigger

    # Given: the example registry (eth-rc4, eth-main, multicore-master) and last night's green
    cfg_path = write_config(tmp_path, monkeypatch)
    example_registry(tmp_path / "registry")
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
                 "bundle": {"store": "ci", "path": "jobs/1999/artifacts"}},
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
@pytest.mark.skip(reason=GATE2)
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
    client = member_client(cfg_path)
    writes = {(m, r.path) for r in client.app.routes
              for m in getattr(r, "methods", ()) if m not in ("GET", "HEAD", "OPTIONS")}
    assert writes == {("POST", "/configurations/{id}/runs"),
                      ("POST", "/api/v1/configurations/{id}/runs")}
    posted = example_provenance()
    for path in ("/configurations/eth-rc4/runs", "/api/v1/configurations/eth-rc4/runs"):
        client.post(path, json=posted)
    results = [as_doc(o)["fact"] for o in db.latest("t0_result", "t0_result:")]
    assert all(r["run"]["nonce"] != posted["run_nonce"] for r in results)
    assert all(r["acceptance"] != "accepted" for r in results)


@pytest.mark.requirement("D5")
@pytest.mark.skip(reason=GATE2)
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
def _rc4_ctx(tmp_path, nonce):
    """rc4's superproject as the no-ssh gate sees it: one public parent and its .gitmodules."""
    return gates_ctx(nonce=nonce, workdir=tmp_path / "work",
                     parents={"%s/%s@%s" % (ETH_REPO[0], ETH_REPO[1], RC4): {
                         "public": True, "gitmodules": RC4_GITMODULES}})


@pytest.mark.requirement("D6")
@pytest.mark.skip(reason=GATE2)
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
    assert gates.run_verdict(list(items.values())) == ("FAIL", 2)

    # The golden example as published lacks t0.runner.hardened (G6): it must roll up UNVERIFIED.
    raw = {i["id"]: i for i in gates.rollup(parse_rows(example_verdicts()), scope="configuration")}
    assert (raw["t0.runner.hardened"]["verdict"], raw["t0.runner.hardened"]["control"]) == (
        "UNVERIFIED", "missing")

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
                     "bundle": {"store": "ci", "path": "jobs/%d/artifacts" % prov["ci"]["job_id"]}}})
    html = member_client(cfg_path).get("/runs/" + nonce).text
    shown = {a["data-gate"]: a for a in elements_with(html, "data-gate")}
    assert sorted(shown) == sorted(CONFIG_GATES)
    for gid, (verdict, control) in EXPECTED_RC4_ROLLUP.items():
        assert (shown[gid]["data-verdict"], shown[gid]["data-control"]) == (verdict, control), gid


@pytest.mark.requirement("D6")
@pytest.mark.skip(reason=GATE2)
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


# ============================================================================================
# AT-D10  T0 runs on the hardened runner
# ============================================================================================
@pytest.mark.requirement("D10")
@pytest.mark.live
@pytest.mark.skip(reason=GATE2)
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
@pytest.mark.skip(reason=GATE2)
def test_AT_D10_negative_control(tmp_path, monkeypatch):
    """AT-D10 control: one-variable changes to a good job environment each FAIL the gate; a job
    run on a runner outside the allowlist is rejected."""
    from nanosoc_rm.t0 import gates
    gate = gates.GATES["t0.runner.hardened"]

    def ctx(name, env_changes=None, home_file=False, tmp_file=False):
        """A fresh job environment per case, so one case's leftovers never leak into another."""
        home, tmp = tmp_path / name / "home", tmp_path / name / "tmp"
        for d in (home, tmp):
            d.mkdir(parents=True)
        if home_file:
            (home / ".gitconfig").write_text("[user]\n\tname = left by a previous job\n")
        if tmp_file:
            (tmp / "previous-job.log").write_text("left by a previous job\n")
        env = {"CI_COMMIT_REF_PROTECTED": "true", "HOME": str(home), "TMPDIR": str(tmp),
               "USER": SERVICE_ACCOUNT}
        env.update(env_changes or {})
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
                    ("USER", ctx("user", {"USER": "jbloggs"}))):
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
@pytest.mark.skip(reason=GATE2)
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
@pytest.mark.skip(reason=GATE2)
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
    assert not any(quarantine.iterdir())

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
    assert "logs/t0.log:3" in json.dumps(scan)


# ============================================================================================
# AT-F1  Defects and errata are recorded
# ============================================================================================
@pytest.mark.requirement("F1")
@pytest.mark.live
@pytest.mark.skip(reason=GATE2)
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
@pytest.mark.skip(reason=GATE2)
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
@pytest.mark.skip(reason=GATE2)
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
        assert html_escape(exp["evidence"].strip()[:40], quote=False) in html
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
    assert run.input_md5 == RC4_BUILD_SOC_MD5
    assert run.detector_source == {"component": "nanosoc_gen", "commit": FIX_FORM_B}
    assert run.result in ("fired", "clean"), run.result            # never an error
    if run.result == "fired":
        assert set(run.forms) <= {"A", "B"}
    if eth["exposure"]["method"] == "detector":                   # once recorded, it must agree
        agree = {"fired": ("exposed", "affected"), "clean": ("not-affected",)}[run.result]
        assert eth["exposure"]["level"] in agree


@pytest.mark.requirement("F2")
@pytest.mark.skip(reason=GATE2)
def test_AT_F2_negative_control(tmp_path):
    """AT-F2 control: ancestry claiming `exposed` is rejected; a detector the defect lacks is
    refused."""
    from nanosoc_rm.registry import RegistryError, load_registry
    load_registry(example_registry(tmp_path / "good"))     # the pair's good half

    # When: the real invalid example replaces the rc4 erratum. Then: rejected by the schema rule.
    anc = example_registry(tmp_path / "ancestry")
    shutil.copyfile(BAD / "erratum.ancestry-claims-exposed.yaml",
                    anc / "errata" / "eth-rc4" / "ERR-001.yaml")
    with pytest.raises(RegistryError) as ei:
        load_registry(anc)
    assert any(ptr.startswith("/exposure") and "'detector' is a required property" in msg
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


# ============================================================================================
# AT-F3  Every cited commit exists on its canonical host
# ============================================================================================
# Every commit R0 cites, each pushed and reachable from a branch or tag on its canonical host.
R0_REFS = {
    ("nanosoc_arch_tech", ORIGIN_ARCH),                 # GitHub main
    ("nanosoc_gen", ORIGIN_GEN),                        # GitLab main
    ("nanosoc_gen", FIX_FORM_A),                        # GitLab main
    ("nanosoc_gen", FIX_FORM_B),                        # GitLab main; also D1's source
    ("nanosoc-multicore-system", WORKAROUND_BRANCH_ONLY),  # GitLab, branch-only
    ("nanosoc-ethernet-chiplet", IMPORT_DOCS_COMMIT),   # GitHub, reachable only from tag v1.0.0
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
    return row, ctl, ctx.items("t0.registry.refs-resolve")    # refs.jsonl: PROPOSED (G11)


@pytest.mark.requirement("F3")
@pytest.mark.live
@pytest.mark.skip(reason=GATE2)
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
@pytest.mark.skip(reason=GATE2)
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

    # Case 2: a record citing ahb_qspi e908ac8, which exists only on the GitLab copy
    r2 = _registry_r0(tmp_path / "r2")
    d2 = load_yaml(EX / "defect.SL-DEF-0001.example.yaml")
    d2.update(id="SL-DEF-0002", component="ahb_qspi", regression_test=None,
              origin=[{"component": "ahb_qspi", "commit": AHB_QSPI_GITLAB_ONLY}])
    dump_yaml(d2, r2 / "defects" / "SL-DEF-0002.yaml")
    row, _ctl, items = _refs_gate(tmp_path, r2, "r2")
    assert row["status"] == "FAIL"
    (q,) = [i for i in items if i["commit"] == AHB_QSPI_GITLAB_ONLY]
    assert (q["result"], q["host"]) == ("fail", "github")      # judged on the canonical host

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
@pytest.mark.skip(reason=GATE2)
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
        (eth, "docs/tapeout/67-rc4-issue-register.md"), (qs, "docs/DEFECT-cpu-ss-hsel.md")])

    # When: the census runs. PROPOSED (G12): `nanosoc-rm registry imports`.
    cp = rm("registry", "imports", path, "--json")
    report = {r["path"]: r for r in (json.loads(x) for x in cp.stdout.splitlines() if x.strip())}

    # Then 2-3: every source's items match the ledger; nothing pending; exit 0
    assert cp.returncode == 0, cp.stdout
    for p, r in report.items():
        assert (r["missing"], r["extra"], r["pending"]) == ([], [], 0), p
        assert r["census"], "%s: the rule extracted no items" % p

    # Independent census of the one document whose structure is known and committed
    known = report["docs/tapeout/11-known-issues.md"]
    if known["commit"] == IMPORT_DOCS_COMMIT:
        text = anon_show(tmp_path / "eth", ETH_HTTPS, IMPORT_DOCS_COMMIT,
                         "docs/tapeout/11-known-issues.md")
        assert text is not None
        own = tuple(line[3:5] for line in text.splitlines()
                    if len(line) > 5 and line.startswith("## ") and line[4] == ")")
        assert own == KNOWN_ISSUES_KEYS
        assert tuple(known["census"]) == KNOWN_ISSUES_KEYS
    opened = report["docs/tapeout/16-open-defects.md"]
    if opened["commit"] == IMPORT_DOCS_COMMIT:
        assert set(OPEN_DEFECTS_NUMBERED) <= set(opened["census"])
        # 6 numbered items + 2 "found in passing" paragraphs + the 2026-08-12 addendum's
        # "found in passing" defect: the rule must import or explicitly exclude every one.
        assert len(opened["census"]) + len(opened["excluded"]) >= OPEN_DEFECTS_CANDIDATES
        assert all(x["reason"].strip() for x in opened["excluded"])

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
@pytest.mark.skip(reason=GATE2)
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
