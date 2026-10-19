"""Optional read-only credentials for the mirrors of private repos (interfaces.md section 3, Private repos).

Off by default. Per host, `[hosts.<host>] private_read` names ONE read method:

    private_read = "ssh"                                       the service user's existing ssh key, BatchMode
    private_read = { method = "ssh", identity_file = "..." }   one key file only (IdentitiesOnly)
    private_read = { method = "token", token_file = "..." }    a read-scope token file (optional `username`)

    method(config, host) -> PrivateRead | None     the configured method, or None (anonymous only)
    classify(stderr) -> cause                      auth | ssh-key | ssh-host | not-found | network | timeout | other
    RETRY_CAUSES                                   the anonymous failures that a private repo gives

Only the mirror sync uses it, and only for `git fetch` and `git ls-remote` (`gitrun` refuses any other
subcommand while the environment carries `MARKER`, and still refuses every write). The T0 gates never read it:
they judge what anyone can fetch, so they stay anonymous.

Never logged: the token reaches git through a credential helper that reads an environment variable; it is in no
argv, URL, observation, alert or error message. The ssh method passes no secret at all. A store helper the user
configured globally is reset for the host's URL, so git never writes the token to `~/.git-credentials`.
"""
from __future__ import annotations

import os
import re
import shlex
from dataclasses import dataclass
from pathlib import Path

from nanosoc_rm.config import Config, ConfigError
from nanosoc_rm.gitrun import CREDENTIALED_MARKER as MARKER

__all__ = ["PrivateRead", "method", "classify", "CAUSES", "RETRY_CAUSES", "MARKER", "METHODS", "TOKEN_VAR",
           "CREDENTIAL_FILTER"]

METHODS = ("ssh", "token")
TOKEN_VAR = "NANOSOC_RM_PRIVATE_READ_TOKEN"
_TOKEN_USER = {"github": "x-access-token", "gitlab": "oauth2"}
# A credentialed fetch keeps blobs up to 1 MiB: .gitmodules and source arrive with the fetch, so no later lazy
# blob fetch (which would run without these credentials) is needed by the walker or the quality reports.
CREDENTIAL_FILTER = "--filter=blob:limit=1m"
SSH_OPTIONS = ("-o", "BatchMode=yes", "-o", "ConnectTimeout=20", "-o", "PasswordAuthentication=no",
               "-o", "KbdInteractiveAuthentication=no")

# cause -> pattern on git's stderr (first match wins).
CAUSES: tuple[tuple[str, re.Pattern], ...] = tuple((c, re.compile(p, re.I)) for c, p in (
    ("timeout", r"timed out after \d+"),
    ("ssh-key", r"permission denied \(publickey|no supported authentication methods"),
    ("ssh-host", r"host key verification failed|no matching host key"),
    ("auth", r"could not read (username|password)|terminal prompts disabled|authentication failed"
             r"|http basic: access denied|\b40[13]\b|invalid username or password"),
    # A transport failure before not-found: over ssh git always ends with "Could not read from remote repository",
    # so a DNS, refused or unreachable failure would otherwise read as "private or missing".
    ("network", r"could not resolve host(name)?|connection (refused|reset|timed out)|network is unreachable"
                r"|no route to host|operation timed out|failed to connect to"),
    ("not-found", r"repository .*not found|not found|does not exist|does not appear to be a git repository"
                  r"|could not read from remote repository"),
    ("network", r"unable to access"),
))
# Anonymous failures a private repo gives: GitHub and GitLab answer 401 over https ("could not read Username"), and
# some hosts answer "not found" for a project the caller may not see.
RETRY_CAUSES = ("auth", "not-found")


def classify(stderr: str | None) -> str:
    text = stderr or ""
    for cause, rx in CAUSES:
        if rx.search(text):
            return cause
    return "other"


@dataclass(frozen=True)
class PrivateRead:
    """One host's read method. `url(path)` is the URL it reads; `env()` the git environment additions (never
    logged; they include MARKER so gitrun allows only fetch and ls-remote with them)."""
    host: str
    kind: str                                    # "ssh" | "token"
    base: str                                    # ssh: hosts.<host>.ssh; token: hosts.<host>.git
    identity_file: str | None = None
    token_file: str | None = None
    username: str | None = None

    def url(self, path: str) -> str:
        if self.kind == "ssh":
            return "%s%s.git" % (self.base, path) if self.base.endswith(":") else \
                "%s/%s.git" % (self.base.rstrip("/"), path)
        return "%s/%s.git" % (self.base.rstrip("/"), path)

    def env(self) -> dict[str, str]:
        """Environment additions for one credentialed fetch or ls-remote. Reads the token file per call; a
        missing or empty one is a ConfigError naming the host, never the content."""
        env = {MARKER: self.kind}
        if self.kind == "ssh":
            argv = ["ssh", *SSH_OPTIONS]
            if self.identity_file:
                argv += ["-o", "IdentitiesOnly=yes", "-i", self.identity_file]
            env["GIT_SSH_COMMAND"] = shlex.join(argv)
            return env
        try:
            token = Path(self.token_file or "").read_text().strip()
        except OSError as e:
            raise ConfigError("[hosts.%s] private_read token file is unreadable (%s)" % (self.host, e.strerror)) \
                from None
        if not token:
            raise ConfigError("[hosts.%s] private_read token file is empty" % self.host)
        user = self.username or _TOKEN_USER.get(self.host, "x-access-token")
        helper = "!f() { test \"$1\" = get || exit 0; echo username=%s; echo \"password=$%s\"; }; f" % (
            user, TOKEN_VAR)
        base = self.base.rstrip("/")
        n = int(os.environ.get("GIT_CONFIG_COUNT", "0") or 0)
        env.update({TOKEN_VAR: token, "GIT_CONFIG_COUNT": str(n + 2)})
        for i, value in ((n, ""), (n + 1, helper)):     # "" resets any inherited helper (a `store` never sees it)
            env["GIT_CONFIG_KEY_%d" % i] = "credential.%s.helper" % base
            env["GIT_CONFIG_VALUE_%d" % i] = value
        return env


def method(config: Config, host: str) -> PrivateRead | None:
    """The read method configured for `host`, or None (the default: anonymous only)."""
    table = (config.get("hosts") or {}).get(host) or {}
    spec = table.get("private_read")
    if spec is None:
        return None
    if isinstance(spec, str):
        spec = {"method": spec}
    kind = spec.get("method")
    if kind == "ssh":
        base = table.get("ssh")
        if not base:
            raise ConfigError("[hosts.%s] private_read = \"ssh\" needs the host's ssh base (ssh = \"git@...:\")"
                              % host)
        return PrivateRead(host, "ssh", base, identity_file=spec.get("identity_file"))
    if kind == "token":
        base = table.get("git") or table.get("web") or ""
        if not base.startswith(("https://", "http://")):
            raise ConfigError("[hosts.%s] private_read token needs an https git base" % host)
        return PrivateRead(host, "token", base, token_file=spec.get("token_file"), username=spec.get("username"))
    raise ConfigError("[hosts.%s] private_read method must be one of %s" % (host, ", ".join(METHODS)))
