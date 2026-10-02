"""The CI API client (interfaces.md section 10): the protocol, the real GitLab client and the test fake.

    CIClient   the protocol every caller codes against
    GitLabCI   the real one: httpx against `hosts.gitlab.api`, project `ci.project` (URL-encoded path)
    FakeCI     the test seam: FakeCI(pipelines=, jobs=, variables=, artifacts=, fail_trigger_for=, runners=)
    CIError    an HTTP error or a transport failure; `status` is the HTTP status or None, `call` is `METHOD path`

The one outbound write of v1 is GitLabCI.trigger: `POST projects/:id/trigger/pipeline` (A2; WRITE_CALLS in
nanosoc_rm.contracts). Its guard: the project is always the configured `ci.project`, and the trigger token must
be recorded against that same project (`credentials.ci_trigger.project`); anything else is refused before a
request is made. Every other call is a GET with the poller's read token.

Credentials never appear in a URL, a command or a message (interfaces.md section 0.3): the read token travels in
the Authorization header, the trigger token in the POST form body, and CIError names only the call and the status.
"""
from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path
from typing import Any, Mapping, Protocol
from urllib.parse import quote

import httpx

from nanosoc_rm.config import Config, load_config

__all__ = ["CIError", "CIClient", "GitLabCI", "FakeCI", "make_client", "project_path", "TIMEOUT_S"]

TIMEOUT_S = 60.0


class CIError(Exception):
    """A CI API call that failed. `status` is the HTTP status (None for a transport error or timeout)."""

    def __init__(self, call: str, status: int | None = None, *, timeout: bool = False, detail: str = ""):
        self.call, self.status, self.timeout = call, status, timeout
        what = ("HTTP %d" % status) if status is not None else ("a timeout" if timeout else "no answer")
        super().__init__("%s answered %s%s" % (call, what, (" (%s)" % detail) if detail else ""))

    @property
    def infra(self) -> bool:
        """An infrastructure cause (D7): a 5xx, a timeout or no answer at all."""
        return self.status is None or self.status >= 500


class CIClient(Protocol):
    """interfaces.md section 10. Every method raises CIError on failure; records are GitLab's own shapes."""

    def trigger(self, ref: str, variables: dict[str, str]) -> dict: ...      # POST projects/:id/trigger/pipeline
    def pipeline(self, pid: int) -> dict: ...                                  # GET projects/:id/pipelines/:pid
    def jobs(self, pid: int) -> list[dict]: ...                                # GET .../pipelines/:pid/jobs
    def variables(self, pid: int) -> list[dict]: ...                           # GET .../pipelines/:pid/variables
    def artifacts(self, job_id: int) -> bytes: ...                             # GET .../jobs/:job_id/artifacts
    def runner(self, runner_id: int) -> dict: ...                              # GET runners/:id (D10)

    def command(self, method: str, what: str, *ids: int) -> str: ...          # the `METHOD path` of a call (A3)


def project_path(config: Config) -> str:
    """`projects/<url-encoded ci.project>`: how the API addresses the CI project (interfaces.md section 17.1)."""
    return "projects/%s" % quote(config.require("ci", "project"), safe="")


_PATHS = {
    "trigger": "{p}/trigger/pipeline",
    "pipeline": "{p}/pipelines/{0}",
    "jobs": "{p}/pipelines/{0}/jobs",
    "variables": "{p}/pipelines/{0}/variables",
    "artifacts": "{p}/jobs/{0}/artifacts",
    "runner": "runners/{0}",
}


class GitLabCI:
    """The real client. Reads the config and the tokens when a call is made, never at construction."""

    def __init__(self, config: Config | None = None, *, timeout: float = TIMEOUT_S):
        self.config = config if config is not None else load_config()
        self.timeout = timeout

    # A3: the exact `METHOD path` of each call, as sources[].command records it.
    def command(self, method: str, what: str, *ids: int) -> str:
        return "%s %s" % (method, _PATHS[what].format(*ids, p=project_path(self.config)))

    def _url(self, what: str, *ids: int) -> str:
        return "%s/%s" % (self.config.host("gitlab")["api"].rstrip("/"),
                          _PATHS[what].format(*ids, p=project_path(self.config)))

    def _get(self, what: str, *ids: int, params: Mapping[str, Any] | None = None) -> httpx.Response:
        call = self.command("GET", what, *ids)
        token = self.config.read_secret("gitlab_read")
        try:
            with httpx.Client(timeout=self.timeout, follow_redirects=True) as http:
                r = http.get(self._url(what, *ids), params=params, headers={"Authorization": "Bearer " + token})
        except httpx.TimeoutException:
            raise CIError(call, timeout=True) from None
        except httpx.HTTPError as e:
            raise CIError(call, detail=type(e).__name__) from None
        if r.status_code >= 400:
            raise CIError(call, r.status_code)
        return r

    def _json(self, what: str, *ids: int, params: Mapping[str, Any] | None = None) -> Any:
        r = self._get(what, *ids, params=params)
        try:
            return r.json()
        except ValueError:
            raise CIError(self.command("GET", what, *ids), r.status_code, detail="not JSON") from None

    def trigger(self, ref: str, variables: dict[str, str]) -> dict:
        """The one write of v1 (A2). Guarded: the project is ci.project, and the trigger token is recorded
        against that project."""
        call = self.command("POST", "trigger")
        project = self.config.require("ci", "project")
        recorded = self.config.get("credentials", {}).get("ci_trigger", {}).get("project")
        if recorded != project:
            raise PermissionError("the ci_trigger token is recorded against %s, not the CI project %s: refused"
                                  % (recorded or "no project", project))
        form = {"token": self.config.read_secret("ci_trigger"), "ref": ref}
        form.update({"variables[%s]" % k: v for k, v in variables.items()})
        try:
            with httpx.Client(timeout=self.timeout) as http:
                r = http.post(self._url("trigger"), data=form)
        except httpx.TimeoutException:
            raise CIError(call, timeout=True) from None
        except httpx.HTTPError as e:
            raise CIError(call, detail=type(e).__name__) from None
        if r.status_code >= 400:
            raise CIError(call, r.status_code)
        try:
            return r.json()
        except ValueError:
            raise CIError(call, r.status_code, detail="not JSON") from None

    def pipeline(self, pid: int) -> dict:
        return self._json("pipeline", pid)

    def jobs(self, pid: int) -> list[dict]:
        return self._json("jobs", pid, params={"per_page": 100})

    def variables(self, pid: int) -> list[dict]:
        return self._json("variables", pid)

    def artifacts(self, job_id: int) -> bytes:
        return self._get("artifacts", job_id).content

    def runner(self, runner_id: int) -> dict:
        return self._json("runner", runner_id)


def make_client(config: Config | None = None) -> GitLabCI:
    return GitLabCI(config)


# -------------------------------------------------------------------------------------------------------------
# The test seam
# -------------------------------------------------------------------------------------------------------------
def _zip_dir(path: Path) -> bytes:
    """A directory as the CI serves an artifact: a zip whose entries start with the directory's own name."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for f in sorted(p for p in path.rglob("*") if p.is_file()):
            z.write(f, "%s/%s" % (path.name, f.relative_to(path).as_posix()))
    return buf.getvalue()


class FakeCI:
    """A CI API in memory (interfaces.md section 10). Records are GitLab-shaped dicts keyed by id:

        FakeCI(pipelines={pid: pipeline}, jobs={pid: [job]}, variables={pid: [variable]},
               artifacts={job_id: bytes | zip path | rm-bundle directory}, runners={id: runner},
               fail_trigger_for={configuration: HTTP status, or "timeout"})

    trigger() creates a pipeline (source trigger, the next free id from 1000) and records its variables; it
    fails for a configuration named in fail_trigger_for. `triggered` lists every accepted trigger call."""

    def __init__(self, pipelines: dict | None = None, jobs: dict | None = None, variables: dict | None = None,
                 artifacts: dict | None = None, fail_trigger_for: dict | None = None, runners: dict | None = None,
                 project: str = "soclabs/nanosoc-compat-ci", project_id: int | None = None):
        self.pipelines = dict(pipelines or {})
        self.job_lists = dict(jobs or {})
        self.variable_lists = dict(variables or {})
        self.artifact_store = dict(artifacts or {})
        self.fail_trigger_for = dict(fail_trigger_for or {})
        self.runners = dict(runners or {})
        self.project, self.project_id = project, project_id
        self.triggered: list[tuple[str, dict[str, str]]] = []

    def command(self, method: str, what: str, *ids: int) -> str:
        return "%s %s" % (method, _PATHS[what].format(*ids, p="projects/%s" % quote(self.project, safe="")))

    def trigger(self, ref: str, variables: dict[str, str]) -> dict:
        call = self.command("POST", "trigger")
        try:
            cfg = json.loads(variables.get("RM_REQUEST", "{}")).get("configuration")
        except ValueError:
            cfg = None
        if cfg in self.fail_trigger_for:
            status = self.fail_trigger_for[cfg]
            raise CIError(call, timeout=True) if status == "timeout" else CIError(call, int(status))
        pid = max([999, *self.pipelines]) + 1
        self.pipelines[pid] = {"id": pid, "project_id": self.project_id, "ref": ref, "sha": "0" * 39 + "1",
                               "source": "trigger", "status": "created"}
        self.variable_lists[pid] = [{"key": k, "value": v, "variable_type": "env_var"} for k, v in variables.items()]
        self.job_lists.setdefault(pid, [])
        self.triggered.append((ref, dict(variables)))
        return {"id": pid, "ref": ref, "status": "created", "source": "trigger"}

    def _get(self, table: dict, key: int, what: str) -> Any:
        if key not in table:
            raise CIError(self.command("GET", what, key), 404)
        return table[key]

    def pipeline(self, pid: int) -> dict:
        return self._get(self.pipelines, pid, "pipeline")

    def jobs(self, pid: int) -> list[dict]:
        return self._get(self.job_lists, pid, "jobs")

    def variables(self, pid: int) -> list[dict]:
        return self._get(self.variable_lists, pid, "variables")

    def artifacts(self, job_id: int) -> bytes:
        a = self._get(self.artifact_store, job_id, "artifacts")
        if isinstance(a, (bytes, bytearray)):
            return bytes(a)
        p = Path(a)
        return _zip_dir(p) if p.is_dir() else p.read_bytes()

    def runner(self, runner_id: int) -> dict:
        r = self._get(self.runners, runner_id, "runner")
        if isinstance(r, int):                    # an HTTP status the runner API answers with
            raise CIError(self.command("GET", "runner", runner_id), r)
        return r
