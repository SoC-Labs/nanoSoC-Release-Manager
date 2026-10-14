"""Shared pieces of the publish unit: errors, the `[stores.artifactory]` settings, readiness, where things live."""
from __future__ import annotations

import dataclasses
import datetime as dt
import os
import re
import shutil
import stat
from pathlib import Path

from nanosoc_rm import clock
from nanosoc_rm.config import Config

__all__ = ["PublishUsage", "PublishRefused", "PublishError", "Settings", "settings", "readiness", "can_publish",
           "publish_dir", "preview_path", "DEFAULT_RELEASE_REPO", "RELEASE_STATUS", "TAPEOUT_STATUS",
           "TAPEOUT_STATES", "MANIFEST_NAME", "tapeout_name", "BUILD_PREFIX", "token", "EXPIRY_WARNING_DAYS"]

DEFAULT_RELEASE_REPO = "asic-release"
MANIFEST_NAME = "release-manifest.json"
BUILD_PREFIX = {"release": "rm-release-", "tapeout": "rm-tapeout-"}
RELEASE_STATUS = "RELEASED"
TAPEOUT_STATUS = "TAPED-OUT"
TAPEOUT_STATES = ("draft", "submitted", "accepted", "fabricated")
EXPIRY_WARNING_DAYS = 14
_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")


class PublishUsage(ValueError):
    """A request refused before any work (exit 3): unknown draft, bad id, missing --confirm."""


class PublishRefused(RuntimeError):
    """The publish was refused (exit 1): a path that exists with other bytes, a digest that differs from the
    confirmed preview, a draft that is not ready, a checksum or signature that does not verify."""


class PublishError(RuntimeError):
    """It could not answer (exit 2): the store is unreachable, no token, no signing key."""


def tapeout_name(tapeout_id: str) -> str:
    return "tapeout-%s.json" % tapeout_id


@dataclasses.dataclass(frozen=True)
class Settings:
    url: str | None
    release_repo: str
    token_file: str | None
    token_expires: str | None
    publishers: tuple[str, ...]
    shared_logins: tuple[str, ...]
    signing_key: str | None
    allowed_signers: str | None
    signer_identity: str
    ssh_keygen: str
    projects: dict

    def project(self, component: str) -> str:
        """The store project folder for a component (`[stores.artifactory] projects`), else the component id."""
        return str(self.projects.get(component) or component)

    def allowed_file(self) -> str | None:
        """The allowed signers file, else the signing key's `.pub` beside it."""
        if self.allowed_signers:
            return self.allowed_signers
        return (self.signing_key + ".pub") if self.signing_key else None


def settings(config: Config) -> Settings:
    a = (config.get("stores") or {}).get("artifactory") or {}
    keygen = a.get("ssh_keygen") or shutil.which("ssh-keygen") or "ssh-keygen"
    return Settings(url=a.get("url"), release_repo=a.get("release_repo") or DEFAULT_RELEASE_REPO,
                    token_file=a.get("token_file"), token_expires=a.get("token_expires") or None,
                    publishers=tuple(a.get("publishers") or ()), shared_logins=tuple(a.get("shared_logins") or ()),
                    signing_key=a.get("signing_key"), allowed_signers=a.get("allowed_signers"),
                    signer_identity=a.get("signer_identity") or "nanosoc-rm", ssh_keygen=keygen,
                    projects=dict(a.get("projects") or {}))


def token(config: Config) -> str:
    """The store token (CR-PR1 pattern). Errors name the key, never the content."""
    s = settings(config)
    if not s.token_file:
        raise PublishError("no store token: [stores.artifactory] token_file is not set")
    try:
        value = Path(s.token_file).read_text().strip()
    except OSError as e:
        raise PublishError("the [stores.artifactory] token_file is unreadable (%s)" % e.strerror) from None
    if not value:
        raise PublishError("the [stores.artifactory] token_file is empty")
    return value


def _file_problem(path: str | None, what: str, key: str) -> str | None:
    if not path:
        return "no %s: set [stores.artifactory] %s" % (what, key)
    p = Path(path)
    try:
        st = p.stat()
    except OSError:
        return "the %s file does not exist ([stores.artifactory] %s)" % (what, key)
    if not stat.S_ISREG(st.st_mode):
        return "the %s is not a regular file ([stores.artifactory] %s)" % (what, key)
    if not os.access(p, os.R_OK):
        return "the %s file is not readable by the service user" % what
    if key in ("token_file", "signing_key") and st.st_mode & 0o077:
        return "the %s file is readable by others (mode %o): chmod 600 it" % (what, stat.S_IMODE(st.st_mode))
    if st.st_size == 0:
        return "the %s file is empty" % what
    return None


def readiness(config: Config, login: str | None = None, *, today: dt.date | None = None,
              probe_signer: bool = True) -> list[str]:
    """Why publishing cannot run here now (empty = it can). Calls nothing remote; `login` adds the GUI rule (a
    listed publisher, never a shared login). `probe_signer=False` (the web process) only stats the key and token
    files: it never parses the private key nor starts ssh-keygen, so secrets stay with the publish job's process;
    the job itself (probe_signer=True) also asks whether this host can sign with the key."""
    from nanosoc_rm.publish import signing
    s = settings(config)
    out = []
    if not s.url:
        out.append("no store: set [stores.artifactory] url (on the tool host http://localhost:8082/artifactory)")
    p = _file_problem(s.token_file, "store token", "token_file")
    if p:
        out.append(p)
    if s.token_expires:
        try:
            exp = dt.date.fromisoformat(s.token_expires)
        except ValueError:
            exp = None
        if exp is not None and exp < (today or clock.now().date()):
            out.append("the store token expired on %s: mint a new one" % s.token_expires)
    p = _file_problem(s.signing_key, "signing key", "signing_key")
    if p:
        out.append(p)
    elif probe_signer:
        which, why = signing.backend(s.signing_key, s.ssh_keygen)
        if which is None:
            out.append("this host cannot sign with the key: %s" % why)
    if not s.allowed_file() or not Path(s.allowed_file()).is_file():
        out.append("no public key to verify with: set [stores.artifactory] allowed_signers or put the key's .pub "
                   "beside it")
    if login is not None:
        out += can_publish(config, login)
    return out


def can_publish(config: Config, login: str) -> list[str]:
    s = settings(config)
    if login in s.shared_logins:
        return ["%s is a shared login: a shared login never publishes" % login]
    if login not in s.publishers:
        return ["%s is not in [stores.artifactory] publishers" % (login or "this user")]
    return []


def publish_dir(config: Config) -> Path:
    return config.data_dir / "publish"


def preview_path(config: Config, kind: str, key: str) -> Path:
    """`<data_dir>/publish/previews/<kind>/<key>.json` (key: `<configuration>/<version>` or a tapeout id)."""
    parts = key.split("/")
    if kind not in ("release", "tapeout") or not all(_ID.match(p) for p in parts) or not 1 <= len(parts) <= 2:
        raise PublishUsage("%r is not a %s key" % (key[:80], kind))
    return publish_dir(config) / "previews" / kind / ("%s.json" % "@".join(parts))

