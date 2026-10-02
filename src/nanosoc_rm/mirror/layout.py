"""Mirror layout and the shared plumbing of the mirror and walker modules (interfaces.md section 3).

- One bare repo per catalogue component or external: `<data_dir>/mirrors/<id>.git`.
- Every repo of the component lives in its own namespace `refs/r/<host>/<quote(path, safe="")>/{heads,tags}/*`;
  the namespace KEY used in function arguments is `<host>/<quote(path, safe="")>`.
- Mirror config: reflogs on every ref, never expired, unreachable objects never pruned (B3 REWRITTEN, B8).
- Each repo's default branch, as the host advertised it at the last sync (`git ls-remote --symref`), is kept in the
  mirror's own config under `nanosoc-rm.<key>.defaultbranch`. It is not a ref, so the mirror still holds only heads
  and tags (H6).

Credentials (interfaces.md sections 0.3 and 3): a host's read token reaches git through a credential helper set in
the environment (`GIT_CONFIG_COUNT`), and the helper reads the token from an environment variable. Neither the token
nor the helper is in any argv, so no recorded command carries a credential.
"""
from __future__ import annotations

import os
import secrets
import urllib.parse
from pathlib import Path

import httpx

from nanosoc_rm import clock, db
from nanosoc_rm.config import Config
from nanosoc_rm.gitrun import GitResult, run_git

__all__ = ["repo_key", "ref_prefix", "split_key", "mirror_path", "ensure_mirror", "default_branch",
           "set_default_branch", "git_env", "git_url", "snapshot_id", "alert_once", "api_get", "ApiAnswer",
           "run_in", "MIRROR_CONFIG", "FETCH_TIMEOUT_S", "API_TIMEOUT_S"]

MIRROR_CONFIG = (("core.logAllRefUpdates", "always"), ("gc.reflogExpire", "never"),
                 ("gc.reflogExpireUnreachable", "never"), ("gc.pruneExpire", "never"))
FETCH_TIMEOUT_S = 600
API_TIMEOUT_S = 30
_CREDENTIAL_USER = {"github": "x-access-token", "gitlab": "oauth2"}   # what each host accepts with a token
_TOKEN_VAR = "NANOSOC_RM_GIT_TOKEN"
_CONFIG_SECTION = "nanosoc-rm"


def repo_key(host: str, path: str) -> str:
    """`<host>/<quote(path, safe="")>`: one repo's namespace key in its component mirror."""
    return "%s/%s" % (host, urllib.parse.quote(path, safe=""))


def ref_prefix(host: str, path: str) -> str:
    """`refs/r/<key>`: the prefix of one repo's heads and tags in its component mirror."""
    return "refs/r/" + repo_key(host, path)


def split_key(key: str) -> tuple[str, str]:
    """(host, path) of a namespace key."""
    host, _, quoted = key.partition("/")
    return host, urllib.parse.unquote(quoted)


def mirror_path(config: Config, entry_id: str) -> Path:
    return config.mirrors_dir / ("%s.git" % entry_id)


def ensure_mirror(config: Config, entry_id: str) -> Path:
    """The component's bare mirror, created with the section 3 config on first use. Never re-created."""
    path = mirror_path(config, entry_id)
    if not (path / "HEAD").is_file():
        path.parent.mkdir(parents=True, exist_ok=True)
        run_git("init", "--quiet", "--bare", path).check()
    for key, value in MIRROR_CONFIG:
        if run_git("config", "--get", key, cwd=path).stdout.strip() != value:
            run_git("config", key, value, cwd=path).check()
    return path


def _branch_key(key: str) -> str:
    return "%s.%s.defaultbranch" % (_CONFIG_SECTION, key)


def default_branch(mirror: Path, key: str) -> str | None:
    """The repo's default branch as last advertised by its host, or None when never learned."""
    if not (Path(mirror) / "HEAD").is_file():
        return None
    r = run_git("config", "--get", _branch_key(key), cwd=mirror)
    return (r.stdout.strip() or None) if r.ok else None


def set_default_branch(mirror: Path, key: str, branch: str) -> None:
    run_git("config", _branch_key(key), branch, cwd=mirror).check()


def git_url(config: Config, host: str, path: str) -> str:
    """The URL the service fetches a repo from: `hosts.<host>.git` (default `web`) + `/<path>.git`."""
    return "%s/%s.git" % (config.host(host)["git"].rstrip("/"), path)


def git_env(config: Config, host: str) -> dict[str, str]:
    """Environment additions for git calls that may reach `host`: its read token through a credential helper,
    when `credentials.<host>_read` is configured. Empty otherwise (anonymous)."""
    name = "%s_read" % host
    if name not in config.get("credentials", {}) or "token_file" not in config["credentials"][name]:
        return {}
    base = config.host(host)["git"].rstrip("/")
    if not base.startswith(("https://", "http://")):
        return {}
    token = config.read_secret(name)
    helper = ("!f() { test \"$1\" = get || exit 0; echo username=%s; echo \"password=$%s\"; }; f"
              % (_CREDENTIAL_USER.get(host, "x-access-token"), _TOKEN_VAR))
    n = int(os.environ.get("GIT_CONFIG_COUNT", "0") or 0)
    env = {_TOKEN_VAR: token, "GIT_CONFIG_COUNT": str(n + 2)}
    for i, value in ((n, ""), (n + 1, helper)):                 # "" resets any inherited helper for this URL
        env["GIT_CONFIG_KEY_%d" % i] = "credential.%s.helper" % base
        env["GIT_CONFIG_VALUE_%d" % i] = value
    return env


def snapshot_id(scope: str) -> str:
    """A new, unique snapshot id for one producer run of `scope`."""
    return "%s@%s#%s" % (scope, clock.iso(), secrets.token_hex(4))


def alert_once(alert_kind: str, about: str, message: str, sources: list[dict], *, config: Config) -> dict | None:
    """Raise an alert unless one with the same subject was already raised (open, or cleared by a person: a
    cleared alert is acknowledged and is not raised again for the same cause). Returns the stored alert or None."""
    subject = "alert:%s:%s" % (alert_kind, about)
    if any(a["subject"] == subject for a in db.alerts(open_only=False, config=config)):
        return None
    return db.raise_alert(alert_kind, about, message, sources, config=config)


# ---------------------------------------------------------------------------------------------------------
# Host API reads (GET only: interfaces.md section 0.4)
# ---------------------------------------------------------------------------------------------------------
class ApiAnswer:
    """One GET: status (None when the host could not be reached), JSON body (None if not JSON), headers, and the
    observation source (`METHOD path`, no credentials)."""

    def __init__(self, status: int | None, body, headers: dict[str, str], source: dict, error: str | None = None):
        self.status, self.body, self.headers, self.source, self.error = status, body, headers, source, error


def _token_headers(config: Config, host: str) -> dict[str, str]:
    name = "%s_read" % host
    if name in config.get("credentials", {}) and "token_file" in config["credentials"][name]:
        return {"Authorization": "Bearer %s" % config.read_secret(name)}
    return {}


def api_get(config: Config, host: str, path: str, params: dict | None = None, *, url: str | None = None,
            client: httpx.Client | None = None) -> ApiAnswer:
    """GET `<hosts.<host>.api>/<path>` (or the absolute `url`, e.g. a pagination link) with the host's read
    token. Never raises for an HTTP or network failure: the answer says what happened."""
    base = config.host(host)["api"].rstrip("/")
    target = url or "%s/%s" % (base, path.lstrip("/"))
    shown = target[len(base) + 1:] if target.startswith(base + "/") else target
    if params:
        query = urllib.parse.urlencode(params)
        target = target + ("&" if "?" in target else "?") + query
        shown = shown + ("&" if "?" in shown else "?") + query
    source = {"kind": "%s_api" % host, "command": "GET %s" % shown}
    headers = {"Accept": "application/json", **_token_headers(config, host)}
    own = client is None
    client = client or httpx.Client(timeout=API_TIMEOUT_S, follow_redirects=True)
    try:
        resp = client.get(target, headers=headers)
    except httpx.HTTPError as e:
        return ApiAnswer(None, None, {}, source, "%s: %s" % (type(e).__name__, e))
    finally:
        if own:
            client.close()
    try:
        body = resp.json()
    except ValueError:
        body = None
    return ApiAnswer(resp.status_code, body, {k.lower(): v for k, v in resp.headers.items()}, source)


def run_in(mirror: Path, *args: str, env: dict | None = None, timeout: float | None = 300) -> GitResult:
    """A git call inside a mirror (recorded as `git -C <mirror> ...`)."""
    return run_git(*args, cwd=mirror, env=env, timeout=timeout)
