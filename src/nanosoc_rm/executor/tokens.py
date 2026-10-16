"""Executor tokens (interfaces.md section 27.5).

A token is 32 random bytes as 64 hex characters, prefixed `rmx_` so a leaked one is recognisable. The service
keeps only its sha256 (`[[executor.agents]] token_sha256`); the agent keeps the token in a 0600 file. With 256 bits
of entropy a plain sha256 is enough (no slow hash needed), and the comparison is constant-time.

Revoking: set `revoked = true` on the agent's entry, or delete the entry. The service config is read per request,
so the next call of that agent is refused (401) without a restart.

    new_token() -> str
    digest(token) -> str
    identify(config, authorization_header) -> dict | None    the [[executor.agents]] entry, or None
"""
from __future__ import annotations

import hashlib
import hmac
import re
import secrets

from nanosoc_rm.config import Config

__all__ = ["new_token", "digest", "identify", "TOKEN_PREFIX", "TOKEN_RE"]

TOKEN_PREFIX = "rmx_"
TOKEN_RE = re.compile(r"^rmx_[0-9a-f]{64}\Z")


def new_token() -> str:
    return TOKEN_PREFIX + secrets.token_hex(32)


def digest(token: str) -> str:
    return hashlib.sha256(token.encode("ascii")).hexdigest()


def identify(config: Config, authorization: str | None) -> dict | None:
    """The configured, unrevoked agent whose token is the request's `Authorization: Bearer <token>`. Every entry
    is compared (constant time each), so the answer time does not depend on which entry matched."""
    if not authorization:
        return None
    scheme, _, token = authorization.partition(" ")
    token = token.strip()
    if scheme.lower() != "bearer" or not TOKEN_RE.match(token):
        return None
    want = digest(token).encode("ascii")
    found = None
    for agent in ((config.get("executor") or {}).get("agents") or []):
        have = str(agent.get("token_sha256", "")).encode("ascii", "replace")
        if hmac.compare_digest(have, want) and found is None:
            found = agent
    if found is None or found.get("revoked"):
        return None
    return found
