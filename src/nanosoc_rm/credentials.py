"""`nanosoc-rm credentials check` (interfaces.md sections 1 and 17.3; H3, A2, PR8).

    check(config=None) -> [credential_status observation], one per credential, stored as one snapshot

v1 has exactly four credentials, each with the scope it may hold:

    github_read   contents:read, metadata:read, members:read   (poller, fine-grained)
    gitlab_read   read_api, read_repository                    (poller, CI fetch)
    ci_trigger    trigger, recorded against ci.project         (Run T0: the one write)
    github_oauth  read:org                                     (login)

Each is judged against its record and, where the host can say, against the host itself:

- gitlab_read: `GET personal_access_tokens/self`. The live scopes must equal the record and stay within the
  role; the token must be active and not revoked; GitLab's expiry must equal the recorded one.
- github_read: `GET user`. A classic token announces itself with an `X-OAuth-Scopes` header: a finding (a classic
  token cannot be read-only on private repos). A fine-grained token cannot report its permissions, so the row is
  `unverified` with that reason; its `github-authentication-token-expiration` header, if any, must equal the record.
- ci_trigger: a trigger token cannot be described with a read token: `unverified`, after checking that its
  record names the configured `ci.project`.
- github_oauth: GitHub does not report an OAuth app's scope; the record must be exactly read:org, which is what
  /auth/login asks for.

Any other credential name is a finding: Phase 4's bump_writer and bot_tag need the Phase 4 entry gate (A7), and
nothing else is allowed in v1 (A2). A v1 credential missing from the config is a finding. A recorded expiry within
14 days raises a `token_expiry` alert; an expired token is a finding. A row is never `ok` without a check
(github_oauth's check is its record, as above). Exit 0 all ok, 1 any finding, 2 any unverified and no finding.

No token value is ever printed, logged or stored: tokens travel only in the Authorization header.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import asdict, dataclass
from typing import Callable

import click
import httpx

from nanosoc_rm import clock, db
from nanosoc_rm.cliutil import emit, exit_code, json_option, subgroup
from nanosoc_rm.config import Config, ConfigError, load_config

__all__ = ["CredentialStatus", "check", "V1_SCOPES", "LATER_CREDENTIALS", "EXPIRY_WARNING_DAYS", "register"]

V1_SCOPES: dict[str, frozenset[str]] = {
    "github_read": frozenset({"contents:read", "metadata:read", "members:read"}),
    "gitlab_read": frozenset({"read_api", "read_repository"}),
    "ci_trigger": frozenset({"trigger"}),
    "github_oauth": frozenset({"read:org"}),
}
LATER_CREDENTIALS = ("bump_writer", "bot_tag")            # Phase 4 (H3); not before its entry gate
EXPIRY_WARNING_DAYS = 14
TIMEOUT_S = 30.0

# An HTTP GET for the checks: (url, token) -> (status or None, lower-cased headers, JSON body or None).
HttpGet = Callable[[str, str], "tuple[int | None, dict[str, str], object]"]


@dataclass
class CredentialStatus:
    """The fact of one credential_status observation (observation.schema.json#/$defs/credential_status)."""
    name: str
    recorded_scope: list[str]
    live_scope: list[str] | None
    expires: str | None
    project: str | None
    verdict: str            # ok | finding | unverified
    reason: str | None


def _http_get(url: str, token: str) -> tuple[int | None, dict[str, str], object]:
    try:
        with httpx.Client(timeout=TIMEOUT_S, follow_redirects=True) as http:
            r = http.get(url, headers={"Authorization": "Bearer " + token})
    except httpx.HTTPError as e:
        return None, {}, type(e).__name__
    try:
        body = r.json()
    except ValueError:
        body = None
    return r.status_code, {k.lower(): v for k, v in r.headers.items()}, body


class _Row:
    """Builds one CredentialStatus: findings accumulate; unverified only when nothing was found."""

    def __init__(self, name: str, table: dict | None):
        table = table or {}
        exp = table.get("expires") or None
        self.s = CredentialStatus(name=name, recorded_scope=list(table.get("scope") or []), live_scope=None,
                                  expires=exp, project=table.get("project"), verdict="ok", reason=None)
        self.findings: list[str] = []
        self.unverified: str | None = None
        self.notes: list[str] = []
        self.sources: list[dict] = []

    def done(self) -> CredentialStatus:
        if self.findings:
            self.s.verdict, self.s.reason = "finding", "; ".join(self.findings)
        elif self.unverified:
            self.s.verdict, self.s.reason = "unverified", self.unverified
        else:
            self.s.verdict, self.s.reason = "ok", ("; ".join(self.notes) or None)
        return self.s


def _token(config: Config, name: str, row: _Row) -> str | None:
    try:
        return config.read_secret(name)
    except ConfigError as e:
        row.findings.append("its token cannot be read: %s" % str(e).split(": ", 1)[-1])
        return None


def _check_gitlab(config: Config, row: _Row, get: HttpGet) -> None:
    tok = _token(config, "gitlab_read", row)
    row.sources.append({"kind": "gitlab_api", "command": "GET personal_access_tokens/self"})
    if tok is None:
        return
    status, _h, body = get("%s/personal_access_tokens/self" % config.host("gitlab")["api"].rstrip("/"), tok)
    if status is None:
        row.unverified = "GitLab could not be reached (%s)" % body
        return
    if status in (401, 403):
        row.findings.append("GitLab refuses the token (HTTP %d): revoked, expired or wrong" % status)
        return
    if status != 200 or not isinstance(body, dict):
        row.unverified = "GitLab answered HTTP %d to GET personal_access_tokens/self" % status
        return
    live = [str(s) for s in body.get("scopes") or []]
    row.s.live_scope = live
    rec = set(row.s.recorded_scope)
    if set(live) - V1_SCOPES["gitlab_read"]:
        row.findings.append("GitLab reports scope %s, beyond read access" % sorted(set(live) - V1_SCOPES["gitlab_read"]))
    if set(live) != rec:
        row.findings.append("live scope %s differs from the recorded %s" % (sorted(live), sorted(rec)))
    if body.get("active") is False or body.get("revoked") is True:
        row.findings.append("GitLab reports the token inactive or revoked")
    exp = body.get("expires_at")
    if exp and row.s.expires and str(exp)[:10] != row.s.expires:
        row.findings.append("recorded expiry %s, GitLab says %s" % (row.s.expires, str(exp)[:10]))


def _check_github(config: Config, row: _Row, get: HttpGet) -> None:
    tok = _token(config, "github_read", row)
    row.sources.append({"kind": "github_api", "command": "GET user"})
    if tok is None:
        return
    status, headers, body = get("%s/user" % config.host("github")["api"].rstrip("/"), tok)
    if status is None:
        row.unverified = "GitHub could not be reached (%s)" % body
        return
    if status in (401, 403):
        row.findings.append("GitHub refuses the token (HTTP %d): revoked, expired or wrong" % status)
        return
    if "x-oauth-scopes" in headers:
        row.s.live_scope = [s.strip() for s in headers["x-oauth-scopes"].split(",") if s.strip()]
        row.findings.append("a classic token (X-OAuth-Scopes: %s) cannot be read-only on private repos; use a "
                            "fine-grained token" % headers["x-oauth-scopes"])
    gexp = headers.get("github-authentication-token-expiration")
    if gexp and row.s.expires and gexp[:10] != row.s.expires:
        row.findings.append("recorded expiry %s, GitHub says %s" % (row.s.expires, gexp[:10]))
    if status != 200:
        row.unverified = "GitHub answered HTTP %d to GET user" % status
    elif not row.findings:
        row.unverified = "a fine-grained token cannot report its permissions; the recorded scope is unchecked"


def _check_trigger(config: Config, row: _Row) -> None:
    project = config.get("ci", {}).get("project")
    row.sources.append({"kind": "derived", "command": "service config [credentials.ci_trigger] project"})
    if not project:
        row.findings.append("[ci] project is not configured, so the trigger's scope cannot be judged")
    elif row.s.project != project:
        row.findings.append("recorded against %s, not the CI project %s (A2)" % (row.s.project, project))
    row.unverified = "a pipeline trigger token cannot be described with a read token; its project is from the record"


def _check_oauth(row: _Row) -> None:
    row.sources.append({"kind": "derived", "command": "GET /auth/login (authorize URL scope=read:org)"})
    if set(row.s.recorded_scope) == V1_SCOPES["github_oauth"]:
        row.notes.append("GitHub does not report an OAuth app's scope; /auth/login asks for read:org only")


def _check_expiry(row: _Row, today: dt.date) -> int | None:
    if row.s.expires is None:
        if row.s.name != "github_oauth":
            row.findings.append("expiry not recorded (H3: warn before any token expires)")
        return None
    try:
        left = (dt.date.fromisoformat(row.s.expires) - today).days
    except ValueError:
        row.findings.append("expiry %r is not a date" % row.s.expires)
        return None
    if left < 0:
        row.findings.append("expired on %s" % row.s.expires)
    return left


def check(config: Config | None = None, *, today: dt.date | None = None, http_get: HttpGet | None = None,
          now: dt.datetime | None = None) -> list[dict]:
    """One credential_status per credential (module docstring), stored as one snapshot `credential_status:all`;
    raises a token_expiry alert for each credential expiring within 14 days. `http_get` is a test seam."""
    config = config if config is not None else load_config()
    get = http_get or _http_get
    stamp = clock.iso(now)
    today = today or clock.parse(stamp).date()
    tables = dict(config.get("credentials", {}))
    rows: list[tuple[_Row, int | None]] = []
    for name in list(V1_SCOPES) + sorted(n for n in tables if n not in V1_SCOPES):
        row = _Row(name, tables.get(name))
        if name not in tables:
            row.findings.append("a v1 credential that is not configured")
        elif name not in V1_SCOPES:
            later = " before the Phase 4 entry gate (A7)" if name in LATER_CREDENTIALS else ""
            row.findings.append("not a v1 credential%s: v1 holds exactly %s (A2, H3)" % (later, ", ".join(V1_SCOPES)))
        else:
            extra = set(row.s.recorded_scope) - V1_SCOPES[name]
            if not row.s.recorded_scope:
                row.findings.append("scope not recorded")
            if extra:
                row.findings.append("recorded scope %s is beyond its role" % sorted(extra))
            if name == "gitlab_read":
                _check_gitlab(config, row, get)
            elif name == "github_read":
                _check_github(config, row, get)
            elif name == "ci_trigger":
                _check_trigger(config, row)
            else:
                _check_oauth(row)
        if not row.sources:
            row.sources.append({"kind": "derived", "command": "nanosoc-rm credentials check"})
        rows.append((row, _check_expiry(row, today) if name in tables else None))

    snap = {"id": db.new_snapshot_id("credential_status:all"), "scope": "credential_status:all"}
    docs = []
    for row, _left in rows:
        fact = asdict(row.done())
        docs.append({"schema": 1, "kind": "credential_status", "subject": "credential_status:%s" % fact["name"],
                     "observed_at": stamp, "sources": row.sources, "snapshot": dict(snap), "fact": fact})
    db.put_snapshot("credential_status:all", docs, config=config)
    open_alerts = {a["subject"] for a in db.alerts(config=config)}
    for row, left in rows:
        if left is not None and left <= EXPIRY_WARNING_DAYS:
            about = "credential_status:%s" % row.s.name
            if "alert:token_expiry:%s" % about not in open_alerts:
                db.raise_alert("token_expiry", about, "%s expires on %s (%s)" % (
                    row.s.name, row.s.expires, "%d days left" % left if left >= 0 else "already expired"),
                    [{"kind": "derived", "command": "nanosoc-rm credentials check"}], at=stamp, config=config)
    return docs


# ---------------------------------------------------------------------------------------------------------------
# CLI (plug-in seam: nanosoc_rm.cli calls register(main))
# ---------------------------------------------------------------------------------------------------------------
_COLUMNS = ("name", "verdict", "recorded_scope", "live_scope", "expires", "reason")


def _table_row(doc: dict) -> dict:
    f = doc["fact"]
    return {"name": f["name"], "verdict": f["verdict"], "recorded_scope": ",".join(f["recorded_scope"]),
            "live_scope": "-" if f["live_scope"] is None else ",".join(f["live_scope"]),
            "expires": f["expires"] or "-", "reason": f["reason"] or ""}


def register(main: click.Group) -> None:
    group = subgroup(main, "credentials", help="The service's credentials and their scopes (H3).")

    @group.command("check")
    @json_option
    def check_cmd(as_json: bool) -> int:
        """Check every credential's scope against its record and, where the host can say, the host (H3).
        Exit 0 all ok, 1 any finding, 2 any unverified."""
        docs = emit(check(), as_json, _COLUMNS, _table_row)
        verdicts = {d["fact"]["verdict"] for d in docs}
        return exit_code(findings="finding" in verdicts, unverified="unverified" in verdicts)
