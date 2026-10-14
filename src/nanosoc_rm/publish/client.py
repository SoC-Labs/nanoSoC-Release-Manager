"""The publish unit's Artifactory client (CR-ART-publish): Bearer token only, and its own method allow-list.

On Artifactory OSS every token is effectively admin (no per-repo permissions), so the TOOL decides what it may do.
Every call is checked here BEFORE any network I/O, and anything else raises `StoreRefused`:

    HEAD, GET   any path under `<url>/` (the base ends in /artifactory and no path has a `..` segment, so the
                Access API is out of reach: Basic-auth calls there count as failed admin logins and lock the admin
                out; the url may not carry user@ credentials either, which httpx would send as Basic auth)
    PUT         `<release_repo>/<project>/<version>/<file>` (a new release record; never overwritten: the
                publish service HEADs first and refuses an existing path) and `api/build` (a build-info record
                named `rm-release-<project>` or `rm-tapeout-<project>`, never a producer's build: check() takes
                the build name, so only put_build can reach it)
    POST        `api/build/promote/<name>/<number>` for those names only (a status-only promotion: the body keys
                are allow-listed, so no copy, no move, no target repo, no properties)

There is no DELETE, PATCH or property-set call. `refuse_writes_here()` (called by `nanosoc-rm serve`) makes every
PUT and POST refuse in the web process: the GUI only queues a publish job, which runs as a child process.

The token travels only in the Authorization header. It is never logged, stored, put in an error or an
observation; an error names the call and its HTTP status only.
"""
from __future__ import annotations

import json
import re
import urllib.parse
from typing import Any, Mapping

import httpx

__all__ = ["Store", "StoreRefused", "StoreError", "refuse_writes_here", "writes_refused", "SAFE_NAME",
           "SAFE_SEGMENT", "prop_value"]

SAFE_SEGMENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.+-]{0,127}\Z")
SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")
_PATH = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.+@=/-]{0,1023}\Z")
_CHECKSUM_SUFFIX = (".md5", ".sha1", ".sha256", ".sha512")
# The only builds this client writes or promotes: the tool's own records (never a producer's build).
BUILD_NAME_PREFIXES = ("rm-release-", "rm-tapeout-")
# A status-only promotion: these keys and no others (`properties` would tag the build's artefacts in other repos).
PROMOTE_KEYS = frozenset(("status", "comment", "ciUser", "timestamp", "dryRun", "copy", "artifacts", "dependencies",
                          "failFast"))
TIMEOUT_S = 60.0
_REFUSE_WRITES: str | None = None


class StoreRefused(PermissionError):
    """The allow-list refused a call before any network I/O."""


class StoreError(RuntimeError):
    """The store answered with an error, or could not be reached. `status` is the HTTP status (None: no answer)."""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


def refuse_writes_here(why: str) -> None:
    """From now on this process refuses every PUT and POST (the web server: it only queues jobs)."""
    global _REFUSE_WRITES
    _REFUSE_WRITES = why


def writes_refused() -> str | None:
    return _REFUSE_WRITES


def prop_value(value: Any) -> str:
    """A deploy-time property value, percent-encoded for a matrix parameter (`;` `=` `,` `\\` and spaces would
    otherwise split or end it; Artifactory decodes the value)."""
    return urllib.parse.quote(str(value), safe="._:@+-")


def _clean(rel: str) -> str:
    if not isinstance(rel, str) or not _PATH.match(rel) or ".." in rel.split("/") or "//" in rel:
        raise StoreRefused("refused: %r is not a plain store path" % str(rel)[:120])
    return rel


class Store:
    """One store, one token. `transport` lets tests route the calls to an in-process fake."""

    def __init__(self, url: str, token: str, *, release_repo: str, timeout: float = TIMEOUT_S,
                 transport: httpx.BaseTransport | None = None):
        if not re.match(r"^https?://[^/?#]+(/[^?#]*)?/artifactory\Z", (url or "").rstrip("/")):
            raise StoreRefused("refused: the store url must end in /artifactory")
        if "@" in urllib.parse.urlsplit(url).netloc:
            # httpx would send Basic auth from the url's userinfo instead of the Bearer token
            raise StoreRefused("refused: the store url must not carry credentials (user@host): the token is the "
                               "only credential")
        if not SAFE_SEGMENT.match(release_repo or ""):
            raise StoreRefused("refused: %r is not a repository key" % release_repo)
        if not token or any(c in token for c in "\r\n "):
            raise StoreError("the store token is empty or malformed")
        self.base = url.rstrip("/")
        self.release_repo = release_repo
        self._token = token
        self._http = httpx.Client(timeout=timeout, follow_redirects=False, trust_env=False, transport=transport)
        self.calls: list[tuple[str, str]] = []             # (method, path): what this client did, for the record

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # -- the allow-list -------------------------------------------------------------------------------------
    def check(self, method: str, rel: str, *, build_name: str | None = None) -> str:
        """The URL for an allowed call; StoreRefused for anything else. Pure: no I/O. `PUT api/build` needs the
        record's `build_name`, one of this tool's own (put_build passes it; put_file never can)."""
        rel = _clean(rel)
        if method in ("GET", "HEAD"):
            return "%s/%s" % (self.base, rel)
        if _REFUSE_WRITES and method in ("PUT", "POST"):
            raise StoreRefused("refused: %s in %s (the web process only queues publish jobs)" % (method, _REFUSE_WRITES))
        parts = rel.split("/")
        if method == "PUT":
            if rel == "api/build":
                if not _own_build(build_name):
                    raise StoreRefused("refused: PUT api/build for build %r, not one of this tool's records (%s<project>)"
                                       % (str(build_name)[:80], " or ".join(BUILD_NAME_PREFIXES)))
                return "%s/%s" % (self.base, rel)
            if (len(parts) == 4 and parts[0] == self.release_repo and all(SAFE_SEGMENT.match(p) for p in parts[1:3])
                    and SAFE_NAME.match(parts[3]) and not parts[3].lower().endswith(_CHECKSUM_SUFFIX)):
                return "%s/%s" % (self.base, rel)
            raise StoreRefused("refused: PUT %s is outside %s/<project>/<version>/<file>" % (rel, self.release_repo))
        if method == "POST":
            if len(parts) == 5 and parts[:3] == ["api", "build", "promote"] and all(SAFE_SEGMENT.match(p)
                                                                                     for p in parts[3:]):
                if not _own_build(parts[3]):
                    raise StoreRefused("refused: POST %s promotes a build this tool did not write" % rel)
                return "%s/%s" % (self.base, rel)
            raise StoreRefused("refused: POST %s is not a build promotion" % rel)
        raise StoreRefused("refused: method %s is never used on the store" % method)

    def _headers(self, extra: Mapping[str, str] | None = None) -> dict[str, str]:
        h = {"Authorization": "Bearer " + self._token, "Accept": "application/json"}
        h.update(extra or {})
        return h

    def _fail(self, method: str, rel: str, e: Exception) -> StoreError:
        return StoreError("%s %s: the store could not be reached (%s)" % (method, rel, type(e).__name__))

    # -- reads -------------------------------------------------------------------------------------------------
    def head(self, rel: str) -> tuple[int, dict[str, str]]:
        url = self.check("HEAD", rel)
        self.calls.append(("HEAD", rel))
        try:
            r = self._http.head(url, headers=self._headers())
        except httpx.HTTPError as e:
            raise self._fail("HEAD", rel, e) from None
        return r.status_code, {k.lower(): v for k, v in r.headers.items()}

    def get(self, rel: str, *, params: Mapping[str, str] | None = None) -> httpx.Response:
        url = self.check("GET", rel)
        self.calls.append(("GET", rel))
        try:
            return self._http.get(url, headers=self._headers(), params=params)
        except httpx.HTTPError as e:
            raise self._fail("GET", rel, e) from None

    def get_json(self, rel: str, *, params: Mapping[str, str] | None = None) -> dict | None:
        """The JSON document at rel; None for 404; StoreError for any other failure."""
        r = self.get(rel, params=params)
        if r.status_code == 404:
            return None
        if r.status_code != 200:
            raise StoreError("GET %s: HTTP %d" % (rel, r.status_code), r.status_code)
        try:
            doc = r.json()
        except ValueError:
            raise StoreError("GET %s: the answer is not JSON" % rel, r.status_code) from None
        if not isinstance(doc, dict):
            raise StoreError("GET %s: the answer is not a JSON object" % rel, r.status_code)
        return doc

    def get_bytes(self, rel: str) -> bytes | None:
        r = self.get(rel)
        if r.status_code == 404:
            return None
        if r.status_code != 200:
            raise StoreError("GET %s: HTTP %d" % (rel, r.status_code), r.status_code)
        return r.content

    def storage(self, path: str) -> dict | None:
        """`api/storage/<repo>/<path>`: size and server checksums of a stored file (None: not stored)."""
        return self.get_json("api/storage/" + _clean(path))

    def properties(self, path: str) -> dict[str, list[str]]:
        """The file's properties ({} when it has none or does not exist: the store answers 404 for both)."""
        doc = self.get_json("api/storage/" + _clean(path), params={"properties": ""})
        props = (doc or {}).get("properties") or {}
        return {str(k): [str(x) for x in (v if isinstance(v, list) else [v])] for k, v in props.items()}

    def build(self, name: str, number: str) -> dict | None:
        """The build-info record (its `buildInfo`), or None."""
        doc = self.get_json("api/build/%s/%s" % (_seg(name), _seg(number)))
        return None if doc is None else (doc.get("buildInfo") or {})

    # -- the three writes ---------------------------------------------------------------------------------------
    def put_file(self, rel: str, data: bytes, *, props: Mapping[str, Any] | None = None,
                 checksums: Mapping[str, str] | None = None) -> dict:
        """Deploy one new file with its properties (set only at upload on OSS). The store checks the given
        checksums and refuses a mismatch."""
        url = self.check("PUT", rel)
        matrix = "".join(";%s=%s" % (k, prop_value(v)) for k, v in sorted((props or {}).items())
                         if re.match(r"^[a-z][a-z0-9_.]{0,63}\Z", k))
        hdrs = {"Content-Type": "application/octet-stream"}
        for alg, value in (checksums or {}).items():
            hdrs["X-Checksum-%s" % alg.capitalize()] = value
        self.calls.append(("PUT", rel))
        try:
            r = self._http.put(url + matrix, content=data, headers=self._headers(hdrs))
        except httpx.HTTPError as e:
            raise self._fail("PUT", rel, e) from None
        if r.status_code not in (200, 201):
            raise StoreError("PUT %s: HTTP %d %s" % (rel, r.status_code, _error_text(r)), r.status_code)
        try:
            return r.json() if r.content else {}
        except ValueError:
            return {}

    def put_build(self, build_info: dict) -> None:
        name = build_info.get("name")
        if not _own_build(name) or not SAFE_SEGMENT.match(str(build_info.get("number") or "")):
            raise StoreRefused("refused: build %r is not one of this tool's records (%s<project>)"
                               % (str(name)[:80], " or ".join(BUILD_NAME_PREFIXES)))
        url = self.check("PUT", "api/build", build_name=name)
        self.calls.append(("PUT", "api/build"))
        try:
            r = self._http.put(url, content=json.dumps(build_info).encode(),
                               headers=self._headers({"Content-Type": "application/json"}))
        except httpx.HTTPError as e:
            raise self._fail("PUT", "api/build", e) from None
        if r.status_code not in (200, 201, 204):
            raise StoreError("PUT api/build: HTTP %d %s" % (r.status_code, _error_text(r)), r.status_code)

    def promote(self, name: str, number: str, body: dict) -> None:
        rel = "api/build/promote/%s/%s" % (_seg(name), _seg(number))
        url = self.check("POST", rel)
        extra = sorted(set(body) - PROMOTE_KEYS)
        if (extra or body.get("copy") or body.get("artifacts") or body.get("dependencies")
                or not isinstance(body.get("status"), str)):
            raise StoreRefused("refused: a promotion here only sets a status (no target repo, no copy, no properties%s)"
                               % ("; not allowed: %s" % ", ".join(extra)[:120] if extra else ""))
        self.calls.append(("POST", rel))
        try:
            r = self._http.post(url, content=json.dumps(body).encode(),
                                headers=self._headers({"Content-Type": "application/json"}))
        except httpx.HTTPError as e:
            raise self._fail("POST", rel, e) from None
        if r.status_code not in (200, 201, 204):
            raise StoreError("POST %s: HTTP %d %s" % (rel, r.status_code, _error_text(r)), r.status_code)


def _own_build(name: object) -> bool:
    return isinstance(name, str) and bool(SAFE_SEGMENT.match(name)) and name.startswith(BUILD_NAME_PREFIXES) \
        and len(name) > max(len(p) for p in BUILD_NAME_PREFIXES if name.startswith(p))


def _seg(value: str) -> str:
    if not SAFE_SEGMENT.match(value or ""):
        raise StoreRefused("refused: %r is not a plain path segment" % str(value)[:80])
    return value


def _error_text(r: httpx.Response) -> str:
    """The store's own error message, short (never a header)."""
    try:
        errs = r.json().get("errors") or []
        msg = "; ".join(str(e.get("message", "")) for e in errs if isinstance(e, dict))
    except (ValueError, AttributeError):
        msg = r.text
    return re.sub(r"\s+", " ", msg or "")[:200]
