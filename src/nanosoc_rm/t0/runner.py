"""The T0 runner: `nanosoc-rm t0 run --out rm-bundle`, in the compat-ci job `t0` ONLY (interfaces.md section 9).

The service never imports this module (AT-A4); it pulls the job's bundle and verifies it (D5).

Job contract:

1. **Input** is one pipeline variable, `RM_REQUEST` (result-bundle.schema.json#/$defs/rm_request). Missing or
   invalid: the runner writes no verdict rows (it has no nonce to stamp), says why on stderr and exits 3.
2. **Environment.** HOME and TMPDIR are listed before anything is written (t0.runner.hardened checks they were
   empty at start). Before the first git call every `CI_*TOKEN*`, `CI_*PASSWORD*` and `CI_JOB_JWT*` variable, every
   variable whose value carries a credential (CI_REPOSITORY_URL holds CI_JOB_TOKEN; any URL with a password; any
   value holding the token) and the ssh agent socket are removed from the process environment. The one exception
   is the `@registry` job's single fetch of the private registry (GATE2 Q3): CI_JOB_TOKEN is held in a local
   variable, handed to that one git call through a credential helper's environment (never the argv or a URL),
   then dropped. Every git call is isolated (`GIT_CONFIG_GLOBAL=/dev/null`, `GIT_CONFIG_NOSYSTEM=1`), never
   prompts, and uses ssh BatchMode. Negative controls route only the reserved `.invalid` URLs of their own scratch
   repos (gates.NEGCTL_HOST) with url.insteadOf.
3. **Gates** of the request's scope (`@registry`: the two registry gates; otherwise the eight configuration
   gates), each with its negative control in the same run. When `t0.infra.probe` reports INFRA the
   host-dependent gates are recorded INFRA without running (D7: an unreachable host is never a FAIL).
4. **Output** `rm-bundle/`: `verdicts.tsv` (5 columns, every row stamped with the nonce), `items/<gate>.jsonl`,
   `provenance.json` and `logs/t0.log`.
5. **finalise_bundle** is the last step, whatever happened before it: it scans `rm-bundle/`, deletes every
   flagged or unscannable file, and records the scan in `provenance.scan` (locations only). It is idempotent:
   after_script runs it again, and a second run keeps the first one's findings and never improves its verdict.
   The job uploads `rm-bundle/` with `when: always`, so nothing the scan flags may still be there at upload time
   (D11).
6. **Exit** 0 all PASS or SKIP, 1 any FAIL or UNVERIFIED (a control that did not fire included), 2 INFRA,
   3 no usable RM_REQUEST. The service ignores the exit code and reads the bundle. `--json` prints the verdict
   rows, one JSON document per line, after the bundle is final.

`url_map` (a JSON file {URL: local repo}) is a test-only seam, refused when CI_JOB_ID is set.
"""
from __future__ import annotations

import json
import os
import platform
import shutil
import sys
from pathlib import Path
from typing import Mapping

from nanosoc_rm import __version__, clock
from nanosoc_rm.contracts import validate_gate_item, validate_provenance, validate_rm_request
from nanosoc_rm.t0 import gates as G
from nanosoc_rm.t0.gitwork import git_version

__all__ = ["main", "run", "finalise_bundle", "scrub_environment", "EXIT_OK", "EXIT_FAIL", "EXIT_INFRA",
           "EXIT_NO_REQUEST", "NO_COMMIT"]

EXIT_OK, EXIT_FAIL, EXIT_INFRA, EXIT_NO_REQUEST = 0, 1, 2, 3
NO_COMMIT = "0" * 40          # captured.superproject_commit when the job could not fetch it: verify rejects it
_REGISTRY_HELPER = '!f() { echo username=gitlab-ci-token; echo "password=$RM_REGISTRY_TOKEN"; }; f'


class _Log:
    def __init__(self):
        self.lines: list[str] = []

    def __call__(self, msg: str) -> None:
        line = "t0: %s" % G.clean_detail(msg)
        self.lines.append(line)
        print(line, file=sys.stderr)


def scrub_environment(environ: dict | os._Environ = os.environ, keep_registry_token: bool = False) -> str | None:
    """Remove every CI credential variable and the ssh agent socket from `environ` (in place). Returns
    CI_JOB_TOKEN's value when `keep_registry_token` (the @registry job's one registry fetch), else None.
    The value is never logged."""
    token = environ.get("CI_JOB_TOKEN") if keep_registry_token else None
    for k in G.credential_vars(environ) + ["SSH_AUTH_SOCK"]:
        environ.pop(k, None)
    return token


def _listing(path: str | None) -> list[str]:
    if not path or not os.path.isdir(path):
        return ["<missing>"]
    return sorted(os.listdir(path))


def _ci_block(environ: Mapping[str, str]) -> dict:
    def num(k):
        v = environ.get(k, "")
        return int(v) if v.isdigit() and int(v) > 0 else 1
    return {"project": environ.get("CI_PROJECT_PATH") or "local/dry-run", "pipeline_id": num("CI_PIPELINE_ID"),
            "job_id": num("CI_JOB_ID"), "job_name": environ.get("CI_JOB_NAME") or "t0",
            "ref": environ.get("CI_COMMIT_REF_NAME") or "local",
            "harness_sha": environ.get("CI_COMMIT_SHA") or NO_COMMIT}


def _fetch_registry(ctx: G.Context, token: str | None, log: _Log) -> tuple[Path | None, str | None]:
    """The @registry job's one authenticated read: the registry at request.commit, checked out in the work dir."""
    req = ctx.request
    url = ctx.repo_url
    if not url:
        log("registry: no base URL for host %s (CI_SERVER_URL unset?)" % req["repo"]["host"])
        return None, None
    d = ctx.workdir / "registry"
    ctx.git.run("init", "-q", d).check()
    r = ctx.git.run("-c", "credential.helper=" + _REGISTRY_HELPER, "fetch", "-q", "--depth=1", url, req["commit"],
                    cwd=d, env={"RM_REGISTRY_TOKEN": token or ""})
    token = None                                    # used once; dropped (GATE2 Q3)
    if not r.ok:
        log("registry: fetch of %s at %s failed: %s" % (url, req["commit"][:7], (r.stderr or "").strip()[-200:]))
        return None, None
    got = ctx.git.run("rev-parse", "FETCH_HEAD^{commit}", cwd=d)
    if not ctx.git.run("checkout", "-q", "FETCH_HEAD", cwd=d).ok:
        return None, got.stdout.strip() if got.ok else None
    return d, got.stdout.strip() if got.ok else None


def run(out: str | os.PathLike, *, environ: dict | os._Environ = os.environ,
        url_map: Mapping[str, str] | None = None) -> int:
    """The whole job. Writes `out` (rm-bundle/) and returns the exit code."""
    started_at = clock.iso()
    start_listing = {"HOME": _listing(environ.get("HOME")), "TMPDIR": _listing(environ.get("TMPDIR"))}
    log = _Log()
    if url_map and environ.get("CI_JOB_ID"):
        log("--url-map is a test-only seam and is refused in a CI job (CI_JOB_ID is set); no rows written")
        return EXIT_NO_REQUEST
    raw = environ.get("RM_REQUEST")
    if not raw:
        log("RM_REQUEST is not set: no nonce to stamp, so no verdict rows are written")
        return EXIT_NO_REQUEST
    try:
        req_doc = json.loads(raw)
    except ValueError as e:
        log("RM_REQUEST is not JSON (%s): no verdict rows are written" % e)
        return EXIT_NO_REQUEST
    problems = validate_rm_request(req_doc)
    if problems:
        log("RM_REQUEST is not a valid rm_request (%s): no verdict rows are written" % "; ".join(problems[:3]))
        return EXIT_NO_REQUEST
    nonce = req_doc["nonce"]
    request = {k: v for k, v in req_doc.items() if k not in ("schema", "nonce")}
    scope = "registry" if request["configuration"] == "@registry" else "configuration"

    token = scrub_environment(environ, keep_registry_token=(scope == "registry"))
    out = Path(out)
    work = out.parent / (out.name + ".work")
    for d in (out, work):
        if d.exists():
            shutil.rmtree(d)
    (out / "items").mkdir(parents=True)
    (out / "logs").mkdir()
    rows: list[dict] = []
    ctx = G.Context(nonce=nonce, workdir=work, request=request, env=dict(environ),
                    service_account=environ.get("RM_SERVICE_ACCOUNT"), url_map=url_map,
                    start_listing=start_listing)
    log("request %s %s/%s@%s (nonce %s...)" % (request["configuration"], request["repo"]["host"],
                                               request["repo"]["path"], request["commit"][:7], nonce[:8]))
    captured_commit = None
    try:
        if scope == "registry":
            ctx.registry_dir, captured_commit = _fetch_registry(ctx, token, log)
            token = None
        probe_infra = False
        with open(out / "verdicts.tsv", "a") as tsv:
            for gid in G.gates_for(scope):
                if probe_infra and gid in G.HOST_DEPENDENT:
                    pair = [{"utc": clock.iso(), "status": "INFRA", "gate": gid, "nonce": nonce,
                             "detail": "not run: t0.infra.probe could not reach a host"}]
                else:
                    pair = list(G.run_with_control(gid, ctx))
                    (out / "items" / ("%s.jsonl" % gid)).write_text(
                        "".join(json.dumps(i, separators=(",", ":")) + "\n" for i in ctx.items(gid)
                                if not validate_gate_item(i)))
                if gid == "t0.infra.probe" and pair[0]["status"] == "INFRA":
                    probe_infra = True
                for r in pair:
                    tsv.write("\t".join(r[k] for k in ("utc", "status", "gate", "detail", "nonce")) + "\n")
                    tsv.flush()
                    log("%s %s: %s" % (r["status"], r["gate"], r["detail"][:200]))
                rows += pair
        if scope == "configuration":
            cap = ctx.captured()
            captured_commit = cap["superproject_commit"]
            pins = cap["pins"]
        else:
            pins = []
        if captured_commit is None:
            log("the superproject was not fetched: captured.superproject_commit is recorded as %s" % NO_COMMIT)
        prov = {"schema": 1, "tier": "t0", "run_nonce": nonce, "request": request, "ci": _ci_block(environ),
                "started_at": started_at, "finished_at": clock.iso(),
                "captured": {"superproject_commit": captured_commit or NO_COMMIT, "pins": pins},
                "tools": {"git": git_version(), "python": platform.python_version(), "nanosoc_rm": __version__},
                "scan": {"tool": "pending", "verdict": "unverified"}}
        bad = validate_provenance(prov)
        if bad:
            log("provenance does not validate: %s" % "; ".join(bad[:3]))
        (out / "provenance.json").write_text(json.dumps(prov, indent=2) + "\n")
    finally:
        (out / "logs" / "t0.log").write_text("\n".join(log.lines + ["git: " + c for c in ctx.git.log]) + "\n")
        res = finalise_bundle(out)
        print("t0: bundle scan %s, %d file(s), %d finding(s)" % (res.verdict, res.files_scanned, len(res.findings)),
              file=sys.stderr)
    statuses = {r["status"] for r in rows}
    if statuses & {"FAIL", "UNVERIFIED", "WARN"}:
        return EXIT_FAIL
    if "INFRA" in statuses:
        return EXIT_INFRA
    return EXIT_OK


_SEVERITY = {"clean": 0, "finding": 1, "unverified": 2}


def _prior_scan(prov: object) -> dict | None:
    """provenance.scan as an earlier finalise_bundle wrote it, or None (no prior scan: the runner's own
    placeholder has tool "pending")."""
    sc = prov.get("scan") if isinstance(prov, dict) else None
    if not isinstance(sc, dict) or sc.get("tool") == "pending" or sc.get("verdict") not in _SEVERITY:
        return None
    ok = [f for f in sc.get("findings") or [] if isinstance(f, dict) and set(f) == {"rule", "file", "line"}
          and isinstance(f["rule"], str) and isinstance(f["file"], str)
          and (f["line"] is None or isinstance(f["line"], int))]
    return {"verdict": sc["verdict"], "findings": ok}


def finalise_bundle(dir: str | os.PathLike, rules=None, toolkit=None):
    """The job's last step before upload (D11): scan `dir` (this package's rules and $RM_SCAN_RULES, plus the
    toolkit scanner $RM_SCAN_TOOLKIT names), delete every flagged or unscannable file, and record the scan in
    provenance.json's `scan` (locations only, never text). When the scan cannot vouch for the files (a rule not
    armed, a rule file or toolkit copy that cannot be used, a toolkit run that did not finish, a flagged path that
    is not there to delete), every file but provenance.json is deleted.

    Idempotent: the job runs it at the end of `t0 run` and again in after_script. A scan an earlier call
    recorded is merged in, never replaced: its findings are kept and the verdict only gets worse
    (clean < finding < unverified). Returns the ScanResult of the merged scan."""
    from nanosoc_rm.scan.rules import RulesError, rule_set
    from nanosoc_rm.scan.scan import ScanResult, scan_dir
    from nanosoc_rm.scan.toolkit import toolkit as find_toolkit
    root = Path(dir)
    prov_path = root / "provenance.json"
    prov = None
    if prov_path.is_file() and not prov_path.is_symlink():
        try:
            prov = json.loads(prov_path.read_text())
        except ValueError:
            prov = None
    prior = _prior_scan(prov)
    try:
        rs = rules or rule_set()
        tk = toolkit if toolkit is not None else find_toolkit()
    except RulesError as e:
        n = sum(1 for p in root.rglob("*") if p.is_file() or p.is_symlink())
        res = ScanResult("unverified", [], n, {}, "nanosoc-rm scan", "rules unusable: %s" % e)
    else:
        res = scan_dir(root, rs, toolkit=tk)
    verdict, why = res.verdict, res.why
    if prior is not None and _SEVERITY[prior["verdict"]] > _SEVERITY[verdict]:
        verdict, why = prior["verdict"], "an earlier scan of this bundle was %s" % prior["verdict"]
    everything = lambda: [p.relative_to(root).as_posix() for p in root.rglob("*")
                          if (p.is_file() or p.is_symlink()) and p != prov_path]
    doomed = everything() if verdict == "unverified" else list(res.flagged_files)
    missing = [rel for rel in doomed if not ((root / rel).is_symlink() or (root / rel).exists())]
    if missing:                            # a flagged name that names no file here: the scan cannot vouch
        verdict, why = "unverified", "%d flagged path(s) not found to delete" % len(missing)
        doomed = everything()
    for rel in doomed:
        p = root / rel
        if p.is_symlink() or p.is_file():
            p.unlink()
        elif p.is_dir():
            shutil.rmtree(p)
    findings = list(prior["findings"]) if prior else []
    findings += [f for f in res.findings if f not in findings]
    flagged = list(dict.fromkeys(f["file"] for f in findings))
    if isinstance(prov, dict):
        prov["scan"] = {"tool": res.tool, "verdict": verdict, "findings": findings}
        prov_path.write_text(json.dumps(prov, indent=2) + "\n")
    return ScanResult(verdict, findings, res.files_scanned, res.armed, res.tool, why, flagged)


def main(argv: list[str] | None = None) -> int:
    """`python -m nanosoc_rm.t0.runner OUT`: the same as `nanosoc-rm t0 run --out OUT`."""
    args = sys.argv[1:] if argv is None else argv
    return run(args[0] if args else "rm-bundle")


if __name__ == "__main__":
    sys.exit(main())
