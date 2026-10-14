"""Catalogue characterisation (interfaces.md section 23; CR-WF-catalogue): the model and its derived defaults,
loader rule 15, the org scan (anonymous, repo_seen snapshots), the catalogue-change preview and the org-scan job
kind. Every check here is shown able to fail by a planted defect (PR6)."""
from __future__ import annotations

import functools
import importlib.util
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from nanosoc_rm import contracts, db, jobs
from nanosoc_rm.catalogue import model
from nanosoc_rm.catalogue.preview import PreviewUsage, preview
from nanosoc_rm.config import load_config
from nanosoc_rm.registry import RegistryError, load_registry

REPO = Path(__file__).resolve().parents[3]
EXAMPLES = REPO / "schemas" / "examples"
LAYOUT = (("component-catalogue.example.yaml", "catalogue.yaml"),
          ("eligibility.example.yaml", "eligibility.yaml"),
          ("defect.SL-DEF-0001.example.yaml", "defects/SL-DEF-0001.yaml"),
          ("erratum.ETH-R0P0-ERR-001.example.yaml", "errata/eth-rc4/ERR-001.yaml"),
          ("import-ledger.example.yaml", "imports.yaml"))
GIT_ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@example.invalid", "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}


@functools.lru_cache(maxsize=None)
def _at(name: str):
    spec = importlib.util.spec_from_file_location("cat_at_%s" % name, REPO / "tests" / "acceptance" / ("%s.py" % name))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def git(*args, cwd):
    p = subprocess.run(["git", *map(str, args)], cwd=cwd, capture_output=True, text=True,
                       env={**os.environ, **GIT_ENV}, timeout=120)
    assert p.returncode == 0, p.stderr
    return p.stdout


@pytest.fixture
def reg_dir(tmp_path):
    root = tmp_path / "registry"
    for src, dst in LAYOUT:
        (root / dst).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(EXAMPLES / src, root / dst)
    git("init", "-q", "-b", "main", cwd=root)
    git("add", "-A", cwd=root)
    git("commit", "-q", "-m", "registry", cwd=root)
    return root


def _cat_text(root: Path) -> str:
    return (root / "catalogue.yaml").read_text()


def _replace_line(root: Path, cid: str, fn) -> None:
    """Rewrite one component's flow line through the preview's own line reader (a planted defect)."""
    from nanosoc_rm.catalogue.preview import _render, entry_line
    lines = _cat_text(root).split("\n")
    n, lead, doc, tail = entry_line(lines, cid)
    fn(doc)
    lines[n] = lead + _render(doc) + tail
    (root / "catalogue.yaml").write_text("\n".join(lines))


# ---------------------------------------------------------------------------------------------------- model
def test_an_old_entry_derives_its_group_and_role_from_class():
    ch = model.characterise({"id": "x", "class": "soc_ref", "canonical": {"host": "github", "path": "o/x"}})
    assert (ch.group, ch.type, ch.role, ch.top_level) == ("project", None, "implementation", True)
    assert ch.derived == ("group", "type", "role") and ch.unconfirmed == ("group", "type", "role")
    tk = model.characterise({"id": "t", "class": "toolkit", "canonical": {"host": "github", "path": "o/t"}})
    assert (tk.group, tk.type, tk.role, tk.derived) == ("toolkit", None, None, ("group",))
    # Every B1 class has a default group, so no catalogue written before CR-WF-catalogue fails to characterise.
    assert set(model.DEFAULT_GROUP) == set(contracts.load_schemas()[0]["defs"]["$defs"]["component_class"]["enum"])


def test_example_registry_characterises_with_wrapped_by_and_the_gap(reg_dir):
    chars = load_registry(reg_dir).characterisations()
    eth, multi = chars["nanosoc-ethernet-chiplet"], chars["nanosoc-multicore-system"]
    assert (eth.group, eth.type, eth.role, eth.wraps, eth.derived) == (
        "project", "chiplet", "implementation", ("nanosoc-multicore-system",), ())
    assert multi.wrapped_by == ("nanosoc-ethernet-chiplet",) and multi.guessed == ("role",)
    assert chars["ethernet-subsystem-ahb"].wrapped_by == ("nanosoc-multicore-system",)
    assert chars["tidechart"].question and chars["tidechart"].unconfirmed == ("group",)
    assert chars["ahb_qspi"].derived == ("group",) and chars["ahb_qspi"].group == "component"
    # The SoC is wrapped only by an implementation: a gap. A forkable wrapper closes it.
    assert model.socs_without_forkable_project(chars) == ["nanosoc-multicore-system"]
    _replace_line(reg_dir, "nanosoc-ethernet-chiplet", lambda d: d.update(role="forkable"))
    assert model.socs_without_forkable_project(load_registry(reg_dir).characterisations()) == []


# ---------------------------------------------------------------------------------------------------- rule 15
RULE15 = [
    ("wraps-unknown", "nanosoc-ethernet-chiplet", lambda d: d.update(wraps=["no-such"]),
     "/components/10/wraps/0", "not a catalogue component"),
    ("wraps-self", "nanosoc-multicore-system", lambda d: d.update(wraps=["nanosoc-multicore-system"]),
     "/components/11/wraps/0", "wraps itself"),
    ("project-wraps-toolkit", "nanosoc-ethernet-chiplet", lambda d: d.update(wraps=["asic-toolkit"]),
     "/components/10/wraps/0", "a project wraps only soc or subsystem or component; asic-toolkit is group toolkit"),
    ("project-wraps-project", "nanosoc-multicore-system", lambda d: d.update(group="project", type="chip"),
     "/components/10/wraps/0",
     "a project wraps only soc or subsystem or component; nanosoc-multicore-system is group project"),
    ("class-drift", "ahb_qspi", lambda d: d.update(group="infra"),
     "/components/1/group", "group infra with class ip_tech"),
    ("legacy-group-live-class", "tidechart", lambda d: (d.update(group="legacy"), d.pop("question"), d.pop("guessed")),
     "/components/23/group", "group legacy goes with class legacy"),
    ("legacy-class-live-group", "secworks-sha256-project", lambda d: d.update(group="component"),
     "/components/26/group", "group component with class legacy"),
    ("guessed-type-on-component", "tidechart", lambda d: d.update(guessed=["type"]),
     "/components/23/guessed/0", "guessed type on a component entry"),
    ("guessed-wraps-without-wraps", "ethernet-subsystem-ahb", lambda d: d.update(guessed=["wraps"]),
     "/components/6/guessed/0", "guessed wraps on an entry with no wraps"),
    ("guessed-inside-on-project", "nanosoc-ethernet-chiplet", lambda d: d.update(guessed=["inside"]),
     "/components/10/guessed/0", "guessed inside"),
]


def test_the_example_registry_passes_rule_15(reg_dir):
    load_registry(reg_dir)


@pytest.mark.parametrize("name,cid,plant,pointer,message", RULE15, ids=[r[0] for r in RULE15])
def test_rule_15_refuses(reg_dir, name, cid, plant, pointer, message):
    _replace_line(reg_dir, cid, plant)
    with pytest.raises(RegistryError) as e:
        load_registry(reg_dir)
    hits = [p for p in e.value.problems if p.rule == "rule-15"]
    assert [(p.file, p.pointer) for p in hits] == [("catalogue.yaml", pointer)], e.value.problems
    assert message in hits[0].message


# ---------------------------------------------------------------------------------------------------- preview
def _apply(root: Path, patch: str) -> None:
    (root.parent / "c.patch").write_text(patch)
    git("apply", "--check", str(root.parent / "c.patch"), cwd=root)
    git("apply", str(root.parent / "c.patch"), cwd=root)


def test_preview_set_confirm_add_ignore_round_trip(reg_dir):
    before = _cat_text(reg_dir)
    p = preview(reg_dir, [
        {"op": "set", "id": "tidechart", "fields": {"group": "toolkit"}},
        {"op": "confirm", "id": "ahb_qspi", "fields": ["group"]},
        {"op": "confirm", "id": "nanosoc-multicore-system", "fields": ["role"]},
        {"op": "add", "repo": {"host": "github", "path": "SoC-Labs/New-Thing"}, "group": "subsystem",
         "role": "forkable"},
        {"op": "ignore", "repo": {"host": "gitlab", "path": "soclabs/lunch-menu"}, "reason": "not  nanoSoC"},
    ])
    assert p.valid, p.problems
    assert _cat_text(reg_dir) == before                    # the tool wrote nothing
    assert p.patch.startswith("diff --git a/catalogue.yaml b/catalogue.yaml\n")
    assert len(p.changes) == 5 and p.changes[3] == "new-thing: add github:SoC-Labs/New-Thing as subsystem (class ip_tech)"
    _apply(reg_dir, p.patch)
    reg = load_registry(reg_dir)
    chars = reg.characterisations()
    assert (chars["tidechart"].group, chars["tidechart"].guessed, chars["tidechart"].question) == ("toolkit", (), None)
    assert chars["ahb_qspi"].derived == () and chars["ahb_qspi"].group == "component"
    assert chars["nanosoc-multicore-system"].guessed == ()
    assert (chars["new-thing"].type, chars["new-thing"].role) == ("subsystem", "forkable")
    assert reg.resolve_repo({"host": "github", "path": "SoC-Labs/New-Thing"}) == ("new-thing", "canonical")
    assert reg.resolve_repo({"host": "gitlab", "path": "soclabs/lunch-menu"}) == (None, "ignored")
    assert reg.ignored()[-1]["reason"] == "not nanoSoC"
    # The edited line keeps its comment; untouched lines are byte-identical.
    assert "# watch-only (B1)" in _cat_text(reg_dir)


def test_preview_reports_a_catalogue_the_loader_would_refuse(reg_dir):
    p = preview(reg_dir, [{"op": "set", "id": "ahb_qspi", "fields": {"group": "project"}}])
    assert not p.valid and any("'type' is a required property" in x for x in p.problems), p.problems
    p = preview(reg_dir, [{"op": "set", "id": "nanosoc-ethernet-chiplet", "fields": {"wraps": ["asic-toolkit"]}}])
    assert not p.valid and any("[rule-15]" in x for x in p.problems), p.problems
    p = preview(reg_dir, [{"op": "add", "repo": {"host": "github", "path": "SoC-Labs/x"}, "group": "project",
                           "role": "example"}])
    assert not p.valid and any("'type'" in x for x in p.problems)


def test_preview_cannot_edit_a_multi_line_entry(reg_dir):
    text = _cat_text(reg_dir).replace(
        '  - {"id": "hostio4", "class": "ip_tech", "canonical": {"host": "gitlab", "path": "soclabs/hostio4"}}',
        '  - id: hostio4\n    class: ip_tech\n    canonical: {host: gitlab, path: soclabs/hostio4}')
    assert text != _cat_text(reg_dir)
    (reg_dir / "catalogue.yaml").write_text(text)
    p = preview(reg_dir, [{"op": "set", "id": "hostio4", "fields": {"group": "arch"}}])
    assert p.patch == "" and p.problems == ["catalogue.yaml: hostio4 is not a one-line entry, so the preview cannot "
                                            "edit it; edit it by hand"]


@pytest.mark.parametrize("choice", [
    {"op": "set", "id": "no-such", "fields": {"group": "lib"}},
    {"op": "set", "id": "tidechart", "fields": {"class": "toolkit"}},
    {"op": "set", "id": "tidechart", "fields": {"group": "gadget"}},
    {"op": "confirm", "id": "tidechart", "fields": ["colour"]},
    {"op": "add", "repo": {"host": "bitbucket", "path": "x/y"}, "group": "lib"},
    {"op": "add", "repo": {"host": "github", "path": "SoC-Labs/TideChart"}, "id": "tidechart", "group": "lib"},
    {"op": "ignore", "repo": {"host": "github", "path": "SoC-Labs/x"}, "reason": " "},
    {"op": "delete", "id": "tidechart"},
], ids=["unknown-id", "class-not-settable", "bad-group", "bad-confirm", "bad-host", "id-taken", "no-reason", "bad-op"])
def test_preview_refuses_a_bad_choice(reg_dir, choice):
    with pytest.raises(PreviewUsage):
        preview(reg_dir, [choice])


def test_preview_cli_exit_codes_and_out_never_in_the_registry(reg_dir, tmp_path, rm_config):
    exe = shutil.which("nanosoc-rm")
    env = dict(os.environ, NANOSOC_RM_CONFIG=str(rm_config))

    def run(*args, stdin=""):
        return subprocess.run([exe, "catalogue", "preview", *args], input=stdin, capture_output=True, text=True,
                              env=env, timeout=300)
    ok = run("-", "--registry", str(reg_dir), "--out", str(tmp_path / "c.patch"),
             stdin=json.dumps([{"op": "set", "id": "tidechart", "fields": {"group": "toolkit"}}]))
    assert ok.returncode == 0, ok.stderr
    assert ok.stdout == (tmp_path / "c.patch").read_text() and ok.stdout.startswith("diff --git")
    bad = run("-", "--registry", str(reg_dir), "--json",
              stdin=json.dumps([{"op": "set", "id": "ahb_qspi", "fields": {"group": "soc"}}]))
    assert bad.returncode == 1 and json.loads(bad.stdout)["valid"] is False
    inside = run("-", "--registry", str(reg_dir), "--out", str(reg_dir / "x.patch"), stdin="[]")
    assert inside.returncode == 3 and not (reg_dir / "x.patch").exists()
    usage = run("-", "--registry", str(reg_dir), stdin=json.dumps([{"op": "nope"}]))
    assert usage.returncode == 3


def test_preview_moves_an_entry_into_and_out_of_legacy_with_its_class(reg_dir):
    """Review F1: a group change takes the class the new group needs (into legacy, out of it, across the ladder)."""
    p = preview(reg_dir, [{"op": "set", "id": "soctools_flow", "fields": {"group": "legacy"}},
                          {"op": "set", "id": "secworks-sha256-project", "fields": {"group": "component"}},
                          {"op": "set", "id": "tidechart", "fields": {"group": "toolkit"}},
                          {"op": "set", "id": "asic-toolkit", "fields": {"group": "flow"}}])
    assert p.valid, p.problems
    assert p.changes[0] == 'soctools_flow: set group="legacy" (class toolkit -> legacy)'
    assert p.changes[3] == 'asic-toolkit: set group="flow"'          # toolkit is a flow's class: kept
    _apply(reg_dir, p.patch)
    reg = load_registry(reg_dir)
    chars = reg.characterisations()
    assert [(chars[c].group, chars[c].cls) for c in ("soctools_flow", "secworks-sha256-project", "tidechart")] == [
        ("legacy", "legacy"), ("component", "ip_tech"), ("toolkit", "toolkit")]


def test_project_wraps_its_own_component(reg_dir):
    """Review F4: a project card leads with its SoC, then the project-level components it integrates."""
    p = preview(reg_dir, [{"op": "set", "id": "nanosoc-ethernet-chiplet",
                           "fields": {"wraps": ["nanosoc-multicore-system", "tidelink", "tidechart"]}}])
    assert p.valid, p.problems
    _apply(reg_dir, p.patch)
    assert load_registry(reg_dir).characterisations()["tidelink"].wrapped_by == ("nanosoc-ethernet-chiplet",)


def _strip_groups(root: Path) -> None:
    lines = _cat_text(root).split("\n")
    from nanosoc_rm.catalogue.preview import _render, entry_line
    for cid in [c["id"] for c in load_registry(root).components()]:
        n, lead, doc, tail = entry_line(lines, cid)
        for f in ("group", "type", "role", "wraps", "guessed", "question"):
            doc.pop(f, None)
        lines[n] = lead + _render(doc) + tail
    (root / "catalogue.yaml").write_text("\n".join(lines))


def test_gap_check_is_unverified_on_an_uncharacterised_catalogue(reg_dir, rm_config):
    """Review F2: with no stated groups every soc_ref reads as a project, so "no gaps" would measure nothing."""
    chars = load_registry(reg_dir).characterisations()
    assert model.gap_unverified(chars) == []
    _strip_groups(reg_dir)
    chars = load_registry(reg_dir).characterisations()
    assert model.socs_without_forkable_project(chars) == []
    assert model.gap_unverified(chars) == ["nanosoc-ethernet-chiplet", "nanosoc-multicore-system"]
    exe = shutil.which("nanosoc-rm")
    p = subprocess.run([exe, "catalogue", "show", str(reg_dir), "--gaps"], capture_output=True, text=True,
                       env=dict(os.environ, NANOSOC_RM_CONFIG=str(rm_config)), timeout=300)
    assert p.returncode == 2, (p.stdout, p.stderr)
    assert "UNVERIFIED" in p.stderr.strip().splitlines()[-1]


# ---------------------------------------------------------------------------------------------------- org scan
@pytest.fixture
def scan_env(tmp_path):
    at = _at("test_scope_web")
    gh, gl = at.FakeHost(), at.FakeHost()
    cat = {"schema": 1, "components": [
        {"id": "known", "class": "soc_ref", "group": "project", "type": "chip", "role": "forkable",
         "canonical": {"host": "github", "path": "SoC-Labs/Known"},
         "copies": [{"host": "gitlab", "path": "soclabs/known"}]}],
        "ignored": [{"repo": {"host": "github", "path": "SoC-Labs/.github"}, "reason": "profile"}]}
    registry = at.write_registry(tmp_path / "reg", catalogue=cat, eligibility={"schema": 1, "configurations": [
        {"id": "x", "component": "known", "ref": {"branch": "main"}}]})
    cfg = at.write_config(tmp_path / "c.toml", data=tmp_path / "data", registry=registry, github=gh, gitlab=gl)
    gh.route("GET", r"/orgs/SoC-Labs/repos", lambda m, q, hd, b: (200, {}, [
        {"full_name": "SoC-Labs/Known", "private": False, "default_branch": "main"},
        {"full_name": "SoC-Labs/.github", "private": False, "default_branch": "main"},
        {"full_name": "SoC-Labs/nanoSoC-Shiny", "private": False, "default_branch": "main",
         "created_at": "2026-10-13T08:00:00Z", "description": "  a new\n thing ", "fork": True}]))
    gl.route("GET", r"/api/v4/groups/soclabs/projects", lambda m, q, hd, b: (200, {}, [
        {"path_with_namespace": "soclabs/known", "visibility": "public", "default_branch": "main"},
        {"path_with_namespace": "soclabs/lunch-menu", "visibility": "public", "default_branch": None,
         "created_at": "2026-10-01T00:00:00.000Z", "description": None,
         "forked_from_project": {"id": 7, "path_with_namespace": "upstream/lunch"}}]))
    yield at, gh, gl, cfg, registry
    gh.close()
    gl.close()


def test_org_scan_is_anonymous_and_stores_only_uncatalogued_repos(scan_env, monkeypatch):
    from nanosoc_rm.catalogue.orgscan import org_scan
    at, gh, gl, cfg, registry = scan_env
    monkeypatch.setenv("NANOSOC_RM_CONFIG", str(cfg))
    config = load_config(cfg)
    res = org_scan(config=config, store=True)
    assert res.errors == {} and res.listed == {"github": 3, "gitlab": 2}
    facts = {d["subject"]: d["fact"] for d in res.docs}
    assert sorted(facts) == ["repo_seen:github:SoC-Labs/nanoSoC-Shiny", "repo_seen:gitlab:soclabs/lunch-menu"]
    shiny = facts["repo_seen:github:SoC-Labs/nanoSoC-Shiny"]
    assert (shiny["created_on"], shiny["description"], shiny["mentions_nanosoc"], shiny["listing"]) == (
        "2026-10-13", "a new thing", True, "anonymous")
    assert facts["repo_seen:gitlab:soclabs/lunch-menu"]["mentions_nanosoc"] is False
    # Review F5: the listing's fork flag is kept, for the queue's "fork of an upstream project" line.
    assert (shiny["fork"], facts["repo_seen:gitlab:soclabs/lunch-menu"]["fork"]) == (True, True)
    # Tokens are configured for both hosts, and none was sent.
    assert gh.log and gl.log and all("Authorization" not in e["headers"] for e in gh.log + gl.log)
    assert gh.writes() == [] and gl.writes() == []
    stored = db.latest("repo_seen", config=config)
    assert sorted(d["subject"] for d in stored) == sorted(facts)
    first = shiny["first_seen"]
    # A later scan keeps first_seen; once the repo is catalogued its host's snapshot no longer holds it.
    again = org_scan(config=config, store=True)
    assert {d["fact"]["first_seen"] for d in again.docs if "Shiny" in d["subject"]} == {first}
    cat = contracts.load_yaml((registry / "catalogue.yaml").read_text())
    cat["ignored"].append({"repo": {"host": "github", "path": "SoC-Labs/nanoSoC-Shiny"}, "reason": "demo"})
    (registry / "catalogue.yaml").write_text(json.dumps(cat))
    org_scan(config=config, store=True)
    assert [d["subject"] for d in db.latest("repo_seen", config=config)] == ["repo_seen:gitlab:soclabs/lunch-menu"]


def test_org_scan_cli_exit_codes_and_a_failed_host_stores_nothing_for_it(scan_env):
    at, gh, gl, cfg, registry = scan_env
    rc, docs, p = at.cli(cfg, "catalogue", "org-scan", "--store", "--json")
    assert rc == 1 and len(docs) == 2, p.stderr                  # new repos seen: a finding
    gl.route("GET", r"/api/v4/groups/soclabs/projects", lambda m, q, hd, b: (500, {}, {"message": "boom"}))
    rc, docs, p = at.cli(cfg, "catalogue", "org-scan", "--store", "--json")
    assert rc == 2 and "could not list gitlab" in p.stderr.strip().splitlines()[-1]
    assert [d["subject"] for d in docs] == ["repo_seen:github:SoC-Labs/nanoSoC-Shiny"]
    rows = at.sqlite_rows(Path(cfg).parent / "data" / "rm.sqlite",
                          "SELECT scope, count(*) AS n FROM snapshots WHERE kind = 'repo_seen' GROUP BY scope")
    assert {r["scope"]: r["n"] for r in rows} == {"repo_seen:github": 2, "repo_seen:gitlab": 1}
    rc, docs, p = at.cli(cfg, "catalogue", "org-scan", "--host", "github", "--json")
    assert rc == 1 and len(docs) == 1


def test_org_scan_job_kind():
    k = jobs.registered("org-scan")
    assert k is not None and k.args(k.params({})) == ["catalogue", "org-scan", "--store", "--json"]
    assert k.args(k.params({"host": "gitlab"}))[:4] == ["catalogue", "org-scan", "--host", "gitlab"]
    with pytest.raises(ValueError):
        k.params({"host": "bitbucket"})
    seen = {"kind": "repo_seen", "subject": "repo_seen:github:SoC-Labs/x"}
    ok = k.outcome([seen], "org-scan: 1 new repo(s) not in the catalogue; listed (anonymous): github 3")
    assert (ok["verdict"], ok["result"]) == ("PASS", {"new": 1, "repos": ["github:SoC-Labs/x"]})
    assert k.outcome([], "org-scan: 0 new repo(s) ...; could not list gitlab")["verdict"] == "UNVERIFIED"
    # The job's name holds no word a later phase's gate governs (phasegates rule 5a).
    from nanosoc_rm import phasegates
    assert phasegates.automation_phase("org-scan") == [] and phasegates.automation_phase("org_scan") == []
