"""The Artifactory client of the store index (Wave E; interfaces.md section 22).

    settings(config) -> Settings          `[stores.artifactory]`: url, release repo, token file, repos, projects
    StoreClient.from_config(config)       a client with the configured Bearer token
    check_request(method, path, ...)      the method allow-list, applied before any request is made

What the client may do (the tool's own allow-list: on Artifactory OSS every token is effectively admin, so the
server cannot be trusted to refuse a write; this module refuses it first):

    GET, HEAD   any path under the API base, except the Access API (`access/...`) and `api/security/...`
                (tested on the percent-decoded, lower-cased path; an empty, `;`, `.`, `..` or double-encoded
                segment is refused, so `..;/`, `%2e%2e/` and `api//security` cannot reach them)
    POST        `api/search/aql` only (AQL is a read; WRITE_CALLS `read-post`)
    PUT         only through the publish service (`nanosoc_rm.store.publish`, the Wave E publish unit), only to a
                path inside the configured release repository, and never from the web process. This module makes
                no PUT itself: `check_request(..., publish_repo=...)` is the guard that service calls.

Credentials: only `Authorization: Bearer <token>` from `[stores.artifactory] token_file`. Never Basic auth (a Basic
call to the Access API counts as a failed admin login and locks the admin out), never a netrc (`trust_env=False`),
never a redirect (a Bearer header must not follow one to another host). The token never appears in a URL, a
command, an observation, an error or a log: StoreError names the call (`METHOD path`) and the status only.
"""
from __future__ import annotations

import dataclasses
import json
import re
import urllib.parse
from pathlib import Path
from typing import Any

import httpx

from nanosoc_rm.config import Config, ConfigError

__all__ = ["Settings", "settings", "StoreError", "StoreRefused", "StoreClient", "check_request", "AQL_PATH",
           "DEFAULT_RELEASE_REPO", "TIMEOUT_S", "STORE"]

STORE = "artifactory"
AQL_PATH = "api/search/aql"
DEFAULT_RELEASE_REPO = "asic-release"
TIMEOUT_S = 60.0
_REFUSED_PREFIXES = ("access/", "api/security/")
_REPO = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}\Z")


class StoreError(Exception):
    """A store call that failed. `cause`: unreachable | auth | absent | http | config | too-large | refused.
    `status` is the HTTP status (None without one). The message names the call, never a credential."""

    def __init__(self, cause: str, call: str, status: int | None = None, detail: str = ""):
        self.cause, self.call, self.status, self.detail = cause, call, status, detail
        what = ("HTTP %d" % status) if status is not None else cause
        super().__init__("%s: %s%s" % (call, what, (" (%s)" % detail) if detail else ""))


class StoreRefused(StoreError):
    """The allow-list refused a call before any request was made."""

    def __init__(self, call: str, why: str):
        super().__init__("refused", call, None, why)


@dataclasses.dataclass(frozen=True)
class Settings:
    url: str
    release_repo: str
    token_file: str | None
    token_expires: str | None
    repos: tuple[str, ...]
    projects: dict[str, str]                 # catalogue component -> store project folder (the publish unit's key)
    # Trust in a release-repository manifest (shared with the publish unit, CR-ART-cfg): the allowed signers file
    # (or the signing key's .pub), the principal it names, the ssh-keygen for non-RSA keys, the shared logins.
    allowed_signers: str | None = None
    signer_identity: str = "nanosoc-rm"
    ssh_keygen: str = "ssh-keygen"
    shared_logins: tuple[str, ...] = ()


def settings(config: Config) -> Settings:
    """`[stores.artifactory]`; ConfigError naming the key when the table or its url is absent."""
    table = config.require("stores", "artifactory")
    url = config.require("stores", "artifactory", "url").rstrip("/")
    repo = table.get("release_repo") or table.get("repo") or DEFAULT_RELEASE_REPO
    if not _REPO.match(repo):
        raise ConfigError("%s: [stores.artifactory] repo %r is not a repository key" % (config.path, repo))
    repos = tuple(table.get("repos") or ())
    bad = [r for r in repos if not _REPO.match(r)]
    if bad:
        raise ConfigError("%s: [stores.artifactory] repos holds %d entry(ies) that are not repository keys"
                          % (config.path, len(bad)))
    allowed = table.get("allowed_signers") or None
    if not allowed and table.get("signing_key") and Path(str(table["signing_key"]) + ".pub").is_file():
        allowed = str(table["signing_key"]) + ".pub"            # the publish unit's fallback (publish.core)
    return Settings(url=url, release_repo=repo, token_file=table.get("token_file"),
                    token_expires=table.get("token_expires") or None, repos=repos,
                    projects={str(k): str(v) for k, v in (table.get("projects") or {}).items()},
                    allowed_signers=allowed, signer_identity=table.get("signer_identity") or "nanosoc-rm",
                    ssh_keygen=table.get("ssh_keygen") or "ssh-keygen",
                    shared_logins=tuple(table.get("shared_logins") or ()))


def _norm(path: str) -> str:
    """The path as the server will resolve it, percent-decoded, for the allow-list test. Refused: an absolute or
    scheme URL, a backslash, and any segment that is empty, carries a `;` (Tomcat strips `..;` to `..`), decodes to
    `.` or `..`, or still holds a `%` after one decode (a double-encoded dot)."""
    p = (path or "").split("?", 1)[0]
    if "://" in p or p.startswith("/") or "\\" in p:
        raise StoreRefused("? %s" % path[:80], "a path relative to the API base is required")
    parts = [urllib.parse.unquote(s) for s in p.split("/")]
    if any(s in ("", ".", "..") or ";" in s or "%" in s or "/" in s or "\\" in s for s in parts):
        raise StoreRefused("? %s" % path[:80], "a path may not leave the API base (dot, empty or encoded segment)")
    return "/".join(parts)


def check_request(method: str, path: str, *, publish_repo: str | None = None) -> None:
    """Raise StoreRefused unless the allow-list in the module docstring permits `method path`."""
    m = (method or "").upper()
    p = _norm(path)
    call = "%s %s" % (m, p)
    low = p.lower()
    if low.startswith(_REFUSED_PREFIXES) or "/access/api/" in "/" + low or "/api/security/" in "/" + low:
        raise StoreRefused(call, "the Access and security APIs are never called (Basic auth there locks the admin out)")
    if m in ("GET", "HEAD"):
        return
    if m == "POST" and p == AQL_PATH:
        return
    if m == "PUT" and publish_repo:
        if not _REPO.match(publish_repo):
            raise StoreRefused(call, "the release repository is not a repository key")
        if p.startswith(publish_repo + "/") and not p.startswith("api/") and len(p) > len(publish_repo) + 1:
            return
        raise StoreRefused(call, "a publish writes only inside the release repository %s" % publish_repo)
    raise StoreRefused(call, "not on the store client's allow-list (GET, HEAD, POST %s)" % AQL_PATH)


def read_token(config: Config, st: Settings) -> str:
    """The Bearer token. ConfigError naming the key, never the content."""
    if not st.token_file:
        raise ConfigError("%s: [stores.artifactory] token_file is required to read the store" % config.path)
    try:
        value = Path(st.token_file).read_text().strip()
    except OSError as e:
        raise ConfigError("%s: [stores.artifactory] token file is unreadable (%s)" % (config.path, e.strerror)) from None
    if not value or any(c.isspace() for c in value):
        raise ConfigError("%s: [stores.artifactory] token file is empty or not one token" % config.path)
    return value


class StoreClient:
    """GET, HEAD and AQL against one Artifactory API base, with a Bearer token."""

    def __init__(self, url: str, token: str, *, timeout: float = TIMEOUT_S, transport: Any = None):
        if not re.match(r"^https?://[^/@\s]+(/[^\s@]*)?\Z", url or ""):
            raise ConfigError("[stores.artifactory] url must be http(s)://host[:port]/path with no user or token")
        self.url = url.rstrip("/") + "/"
        self._token = token
        kw: dict[str, Any] = {"base_url": self.url, "timeout": timeout, "trust_env": False, "follow_redirects": False,
                              "headers": {"Authorization": "Bearer " + token, "User-Agent": "nanosoc-rm store-index"}}
        if transport is not None:
            kw["transport"] = transport
        self._http = httpx.Client(**kw)

    @classmethod
    def from_config(cls, config: Config, *, transport: Any = None) -> "StoreClient":
        st = settings(config)
        return cls(st.url, read_token(config, st), transport=transport)

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> "StoreClient":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # --- calls ---------------------------------------------------------------------------------------------
    def _fail(self, call: str, r: httpx.Response) -> StoreError:
        cause = "auth" if r.status_code in (401, 403) else "absent" if r.status_code in (400, 404, 405, 501) else "http"
        text = ""
        try:
            text = r.text[:200].replace(self._token, "<token>") if self._token else r.text[:200]
        except Exception:                                         # noqa: BLE001 - a body that cannot decode
            text = ""
        return StoreError(cause, call, r.status_code, " ".join(text.split()))

    def _transport_error(self, call: str, e: Exception) -> StoreError:
        return StoreError("unreachable", call, None, type(e).__name__)

    def get(self, path: str, params: dict | None = None) -> httpx.Response:
        call = "GET %s" % path
        check_request("GET", path)
        try:
            r = self._http.get(path, params=params)
        except httpx.HTTPError as e:
            raise self._transport_error(call, e) from None
        if r.status_code >= 300:
            raise self._fail(call, r)
        return r

    def get_json(self, path: str, params: dict | None = None) -> Any:
        r = self.get(path, params)
        try:
            return r.json()
        except ValueError:
            raise StoreError("http", "GET %s" % path, r.status_code, "the answer is not JSON") from None

    def head(self, path: str) -> int:
        """The status of a HEAD (an existence test: `?properties` cannot tell absent from bare)."""
        call = "HEAD %s" % path
        check_request("HEAD", path)
        try:
            return self._http.head(path).status_code
        except httpx.HTTPError as e:
            raise self._transport_error(call, e) from None

    def aql(self, query: str) -> dict:
        """One AQL query (POST api/search/aql, a read)."""
        call = "POST %s" % AQL_PATH
        check_request("POST", AQL_PATH)
        try:
            r = self._http.post("api/search/aql", content=query.encode(), headers={"Content-Type": "text/plain"})
        except httpx.HTTPError as e:
            raise self._transport_error(call, e) from None
        if r.status_code >= 300:
            raise self._fail(call, r)
        try:
            doc = r.json()
        except ValueError:
            raise StoreError("http", call, r.status_code, "the answer is not JSON") from None
        if not isinstance(doc, dict) or not isinstance(doc.get("results"), list):
            raise StoreError("http", call, r.status_code, "no results list in the answer")
        return doc

    def get_bytes(self, path: str, max_bytes: int) -> bytes:
        """A file's content, at most `max_bytes` (StoreError cause too-large beyond it, read no further)."""
        call = "GET %s" % path
        check_request("GET", path)
        try:
            with self._http.stream("GET", path) as r:
                if r.status_code >= 300:
                    r.read()
                    raise self._fail(call, r)
                out = bytearray()
                for chunk in r.iter_bytes():
                    out += chunk
                    if len(out) > max_bytes:
                        raise StoreError("too-large", call, r.status_code, "over %d bytes" % max_bytes)
                return bytes(out)
        except httpx.HTTPError as e:
            raise self._transport_error(call, e) from None


def dumps(doc: Any) -> str:
    return json.dumps(doc, sort_keys=True, separators=(",", ":"))
