"""Start a T0 run in the CI project (interfaces.md section 10 Trigger; A2, A4, D2, H2).

    trigger(configuration, by, kind, client=None) -> t0_result observation (pending, or no-bundle)

1. Build RM_REQUEST: the commit (a frozen configuration's recorded commit; otherwise its ref resolved in the
   component's mirror, on the canonical host's namespace), `upgrade_from` resolved the same way,
   `canonical_urls` from the latest stored walk, and `nonce = secrets.token_hex(16)`. The `@registry` job asks
   for the registry checkout's `registry.ref` head.
2. Store the RunRecord (`db.put_run`): the request bytes never change afterwards (verify check 2).
3. POST `projects/:id/trigger/pipeline` with `ref = ci.ref` and `variables[RM_REQUEST]`: the only write of v1 (A2).
4. Record the pipeline id. A manual run also writes an `actions` row (`run_t0`, target = configuration) at the
   run's own trigger time (H2, A2).
5. If the POST fails (an HTTP error, a timeout, or a trigger that cannot be sent at all), the run is stored as
   `no-bundle` with `pipeline_id: null`, verdict INFRA for a 5xx, a timeout or no answer, else UNVERIFIED, and a
   reason naming the trigger call and the HTTP status. It is then not pending, so the Run button is enabled
   again and a missed night is visible (D2). An unexpected error in the call is stored the same way
   (UNVERIFIED), then re-raised. A config error found before the call (no `ci.project`) raises ConfigError and
   stores nothing. A run left pending with no pipeline id (the process died during the POST) is made final by
   fetch once the timeout passes.

At most one pending manual run per configuration: a manual trigger while one is pending raises RunPending (the
Run button is disabled in that state). The nightly schedule always triggers, so a stuck run never hides a night;
the stuck run times out in fetch (section 10 Fetch).
"""
from __future__ import annotations

import json
import secrets
from pathlib import Path
from typing import Literal

from nanosoc_rm import clock, contracts, db
from nanosoc_rm.ci import configs
from nanosoc_rm.ci.client import CIClient, CIError, make_client
from nanosoc_rm.ci.verify import RunRecord
from nanosoc_rm.config import Config, ConfigError, load_config
from nanosoc_rm.gitrun import run_git
from nanosoc_rm.mirror import layout

__all__ = ["trigger", "build_request", "canonical_urls_from", "request_bytes", "RunPending", "TriggerError", "pending_runs"]


class RunPending(RuntimeError):
    """A manual trigger while a run of the same configuration is still pending (the Run button is disabled)."""


class TriggerError(RuntimeError):
    """The request could not be built (unknown commit, unreadable mirror or registry). Nothing was stored."""


def _resolve(git_dir: Path, rev: str, what: str) -> tuple[str, dict]:
    r = run_git("rev-parse", "--verify", "--quiet", rev + "^{commit}", cwd=git_dir, timeout=60)
    sha = r.stdout.strip() if isinstance(r.stdout, str) else ""
    if not r.ok or len(sha) != 40:
        raise TriggerError("cannot resolve %s: %s has no %s" % (what, git_dir, rev))
    return sha, r.source()


def _mirror_ref(config: Config, component: str, repo: dict, ref: dict) -> tuple[str, dict]:
    """A branch or tag of `repo`, resolved in the component's mirror (interfaces.md section 3 layout)."""
    kind, name = ("heads", ref["branch"]) if "branch" in ref else ("tags", ref["tag"])
    return _resolve(layout.mirror_path(config, component), "%s/%s/%s" % (layout.ref_prefix(repo["host"], repo["path"]), kind, name),
                    "%s %s of %s:%s" % ("branch" if kind == "heads" else "tag", name, repo["host"], repo["path"]))


def _canonical_urls(config: Config, configuration: str) -> dict[str, str]:
    """RM_REQUEST canonical_urls (result-bundle schema): pinned URL -> the canonical repo's anonymous https URL,
    for each pin of the latest stored walk whose canonical repo is not the one its URL names. Empty when the
    configuration has not been walked yet."""
    return canonical_urls_from(config, db.latest("pin_state", "pin_state:%s:" % configuration, config=config))


def canonical_urls_from(config: Config, pin_docs) -> dict[str, str]:
    """canonical_urls from the given pin_state observations (the same rule as a stored walk's; CR-11 `compat`
    passes its own, possibly overridden, walk)."""
    out: dict[str, str] = {}
    for obs in pin_docs:
        f = obs["fact"]
        for host, h in sorted((f.get("hosts") or {}).items()):
            if h.get("role") != "canonical" or h.get("pinned"):
                continue
            base = config.get("hosts", {}).get(host, {}).get("web", "")
            url = "%s/%s.git" % (base.rstrip("/"), h["repo"]["path"])
            if url.startswith("https://"):
                out[f["url"]] = url
    return out


def build_request(configuration: str, nonce: str, config: Config) -> tuple[dict, list[dict]]:
    """(RM_REQUEST as a dict, the sources of its commit). Raises UnknownConfiguration, TriggerError."""
    sources: list[dict] = []
    if configuration == configs.REGISTRY_JOB:
        repo = configs.registry_repo(config)
        commit, src = _resolve(Path(config.require("registry", "path")), config["registry"].get("ref", "main"),
                               "the registry head")
        sources.append(src)
        upgrade_from, canonical = None, {}
    else:
        cfg = configs.configuration(configuration, config)
        repo = configs.canonical_repo(cfg["component"], config)
        if cfg.get("frozen"):
            commit = cfg["commit"]
            sources.append({"kind": "registry", "command": "eligibility.yaml configurations[id=%s].commit"
                            % configuration})
        else:
            commit, src = _mirror_ref(config, cfg["component"], repo, cfg["ref"])
            sources.append(src)
        upgrade_from = None
        if cfg.get("upgrade_from"):
            upgrade_from, src = _mirror_ref(config, cfg["component"], repo, cfg["upgrade_from"])
            sources.append(src)
        canonical = _canonical_urls(config, configuration)
    req = {"schema": 1, "nonce": nonce, "configuration": configuration, "repo": repo, "commit": commit,
           "upgrade_from": upgrade_from, "canonical_urls": canonical}
    problems = contracts.validate_rm_request(req)
    if problems:
        raise TriggerError("RM_REQUEST for %s does not validate: %s" % (configuration, "; ".join(problems)))
    return req, sources


def request_bytes(req: dict) -> bytes:
    """RM_REQUEST as stored and sent: compact JSON (interfaces.md section 9 Input)."""
    return json.dumps(req, separators=(",", ":")).encode()


def pending_runs(configuration: str | None = None, *, config: Config | None = None) -> list[dict]:
    return db.runs(configuration, state="pending", config=config)


def _result(run: RunRecord, *, acceptance: str, verdict: str | None, reason: str | None, sources: list[dict]) -> dict:
    return {"schema": 1, "kind": "t0_result", "subject": "t0_result:%s:%s" % (run.configuration, run.nonce),
            "observed_at": run.triggered_at, "sources": sources,
            "fact": {"configuration": run.configuration, "commit": run.commit,
                     "run": {"nonce": run.nonce, "trigger": run.trigger, "triggered_by": run.triggered_by,
                             "triggered_at": run.triggered_at, "pipeline_id": run.pipeline_id, "job_id": None},
                     "acceptance": acceptance, "reason": reason, "verdict": verdict, "gates": [], "bundle": None,
                     "started_at": None, "finished_at": None, "over_budget": None, "runner": None}}


def trigger(configuration: str, by: str, kind: Literal["schedule", "manual"], client: CIClient | None = None, *,
            config: Config | None = None) -> dict:
    """Start one T0 run (module docstring). Returns its t0_result: `pending`, or `no-bundle` when the trigger
    failed. Raises UnknownConfiguration, RunPending, TriggerError, ValueError (a bad `by`/`kind`)."""
    config = config if config is not None else load_config()
    if kind not in ("schedule", "manual"):
        raise ValueError("kind must be schedule or manual, not %r" % kind)
    if kind == "schedule" and by != "schedule":
        raise ValueError("a scheduled run is triggered_by 'schedule', not %r (A2)" % by)
    if kind == "manual" and (not by or by == "schedule"):
        raise ValueError("a manual run names the login that started it (H2)")
    if kind == "manual" and pending_runs(configuration, config=config):
        raise RunPending("a run of %s is already pending; Run is disabled until it is fetched" % configuration)
    nonce = secrets.token_hex(16)
    req, sources = build_request(configuration, nonce, config)
    # Everything that can fail on the service's own config is settled before the run is stored, so a config
    # error (exit 3) never leaves a pending run behind (it would disable Run until the fetch timeout).
    client = client if client is not None else make_client(config)
    call = client.command("POST", "trigger")
    ref = config.get("ci", {}).get("ref", "main")
    run = RunRecord(nonce=nonce, configuration=configuration, commit=req["commit"], request=request_bytes(req),
                    trigger=kind, triggered_by=by, triggered_at=clock.iso(), pipeline_id=None, job_id=None,
                    state="pending")
    db.put_run(run, config=config)

    sources = [{"kind": "ci_api", "command": call}] + sources
    acceptance, verdict, reason, unexpected = "pending", None, None, None
    try:
        answer = client.trigger(ref, {"RM_REQUEST": run.request.decode()})
        pid = answer.get("id") if isinstance(answer, dict) else None
        if not isinstance(pid, int) or isinstance(pid, bool) or pid < 1:
            raise CIError(call, detail="the answer names no pipeline id")
        run.pipeline_id = pid
    except CIError as e:
        acceptance, verdict = "no-bundle", ("INFRA" if e.infra else "UNVERIFIED")
        reason = "the trigger failed: %s; no pipeline was started" % e
    except (ConfigError, PermissionError) as e:
        acceptance, verdict = "no-bundle", "UNVERIFIED"
        reason = "the trigger was not sent (%s): %s; no pipeline was started" % (call, e)
    except Exception as e:                               # a bug: the run is still made final, then re-raised
        acceptance, verdict, unexpected = "no-bundle", "UNVERIFIED", e
        reason = "the trigger did not complete (%s: %s); no pipeline id was recorded" % (call, type(e).__name__)
    run.state = acceptance
    db.put_run(run, config=config)
    if kind == "manual":
        db.log_action(by, "run_t0", configuration, acceptance, at=run.triggered_at, config=config)
    doc = _result(run, acceptance=acceptance, verdict=verdict, reason=reason, sources=sources)
    db.put(doc, config=config)
    if unexpected is not None:
        raise unexpected
    return doc
