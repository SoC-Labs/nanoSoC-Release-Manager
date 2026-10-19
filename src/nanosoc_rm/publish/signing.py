"""Manifest signatures in the OpenSSH SSHSIG format, namespace `nanosoc-rm`.

    sign(data, key_path, *, ssh_keygen) -> Signature           the armored `.sig` text and how it was made
    verify(data, armored, allowed, *, ssh_keygen) -> str        the signer's key fingerprint, or SignatureError
    load_allowed(allowed_signers or public key file) -> [AllowedKey]
    backend(key_path, ssh_keygen) -> (name or None, why)       which signer this host can use

The signature is what `ssh-keygen -Y sign -f KEY -n nanosoc-rm FILE` writes beside FILE, and it verifies with
`ssh-keygen -Y verify -f allowed_signers -I <identity> -n nanosoc-rm -s FILE.sig < FILE` on any machine. Two
signers produce it:

- `ssh-keygen`: the configured binary, when it supports `-Y` (OpenSSH 8.1 and later). Any key type.
- `builtin`: this module, for an RSA key (`rsa-sha2-512`, the SSHSIG default hash sha512), on a host whose
  OpenSSH is older (RHEL 8 ships 8.0, which has no `-Y`). Standard library only: no new dependency. The key file
  is PKCS#1 PEM, unencrypted PKCS#8 PEM or an unencrypted OpenSSH private key.

An encrypted key is refused (the publish job runs unattended and never prompts). Neither the key nor its path's
content ever appears in an error or a log. Verification needs no private key: the allowed signers file (or the
key's `.pub`) names the accepted public keys; an RSA signature is checked here, any other type with ssh-keygen.
"""
from __future__ import annotations

import base64
import dataclasses
import functools
import hashlib
import os
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Iterable

__all__ = ["NAMESPACE", "Signature", "AllowedKey", "SignatureError", "sign", "verify", "load_allowed", "backend",
           "public_blob", "fingerprint", "ssh_keygen_supports_y"]

NAMESPACE = "nanosoc-rm"
MAGIC = b"SSHSIG"
ARMOR_BEGIN = "-----BEGIN SSH SIGNATURE-----"
ARMOR_END = "-----END SSH SIGNATURE-----"
_DIGEST_INFO = {"sha512": bytes.fromhex("3051300d060960864801650304020305000440"),
                "sha256": bytes.fromhex("3031300d060960864801650304020105000420")}
_RSA_SIG = {"rsa-sha2-512": "sha512", "rsa-sha2-256": "sha256"}
TIMEOUT_S = 60


class SignatureError(ValueError):
    """A key that cannot sign here, or a signature that does not verify. The message never holds key material."""


@dataclasses.dataclass(frozen=True)
class Signature:
    text: str                 # the armored SSHSIG, as written to `<file>.sig`
    backend: str              # "ssh-keygen" or "builtin"
    fingerprint: str          # SHA256:<base64> of the signing public key


@dataclasses.dataclass(frozen=True)
class AllowedKey:
    principal: str
    blob: bytes               # the SSH wire public key
    namespaces: tuple[str, ...] | None

    @property
    def keytype(self) -> str:
        return _Reader(self.blob).string().decode("ascii", "replace")

    def line(self) -> str:
        """The allowed_signers line ssh-keygen -Y verify reads."""
        opts = ' namespaces="%s"' % ",".join(self.namespaces) if self.namespaces else ""
        return "%s%s %s %s\n" % (self.principal, opts, self.keytype, base64.b64encode(self.blob).decode())


# ---------------------------------------------------------------------------------------------------------
# SSH wire encoding
# ---------------------------------------------------------------------------------------------------------
def _u32(n: int) -> bytes:
    return n.to_bytes(4, "big")


def _string(b: bytes) -> bytes:
    return _u32(len(b)) + b


def _mpint(n: int) -> bytes:
    if n == 0:
        return _string(b"")
    return _string(n.to_bytes(n.bit_length() // 8 + 1, "big"))


class _Reader:
    def __init__(self, data: bytes):
        self.d, self.i = data, 0

    def take(self, n: int) -> bytes:
        if n < 0 or self.i + n > len(self.d):
            raise SignatureError("truncated SSH structure")
        b = self.d[self.i:self.i + n]
        self.i += n
        return b

    def u32(self) -> int:
        return int.from_bytes(self.take(4), "big")

    def string(self) -> bytes:
        return self.take(self.u32())

    def mpint(self) -> int:
        return int.from_bytes(self.string(), "big")

    def done(self) -> bool:
        return self.i == len(self.d)


def fingerprint(blob: bytes) -> str:
    return "SHA256:" + base64.b64encode(hashlib.sha256(blob).digest()).decode().rstrip("=")


def _rsa_blob(e: int, n: int) -> bytes:
    return _string(b"ssh-rsa") + _mpint(e) + _mpint(n)


# ---------------------------------------------------------------------------------------------------------
# Private keys (RSA for the builtin signer; other types are recognised only to say why they cannot sign here)
# ---------------------------------------------------------------------------------------------------------
@dataclasses.dataclass(frozen=True)
class _RsaKey:
    n: int
    e: int
    d: int

    @property
    def blob(self) -> bytes:
        return _rsa_blob(self.e, self.n)


def _pem_body(text: str, label: str) -> bytes | None:
    m = re.search(r"-----BEGIN %s-----\s*(.*?)-----END %s-----" % (label, label), text, re.S)
    if not m:
        return None
    body = m.group(1)
    if "Proc-Type:" in body or "ENCRYPTED" in body:
        raise SignatureError("the signing key is encrypted; the publish job never prompts for a passphrase")
    try:
        return base64.b64decode("".join(ln.strip() for ln in body.splitlines() if ":" not in ln), validate=True)
    except ValueError:
        raise SignatureError("the signing key is not valid PEM") from None


def _der(data: bytes, i: int = 0) -> tuple[int, bytes, int]:
    """(tag, value, next index) of one DER element."""
    if i + 2 > len(data):
        raise SignatureError("truncated DER in the signing key")
    tag, ln = data[i], data[i + 1]
    i += 2
    if ln & 0x80:
        k = ln & 0x7F
        if k == 0 or k > 4 or i + k > len(data):
            raise SignatureError("bad DER length in the signing key")
        ln = int.from_bytes(data[i:i + k], "big")
        i += k
    if i + ln > len(data):
        raise SignatureError("truncated DER in the signing key")
    return tag, data[i:i + ln], i + ln


def _der_seq(data: bytes) -> list[tuple[int, bytes]]:
    tag, body, _ = _der(data)
    if tag != 0x30:
        raise SignatureError("the signing key is not a DER sequence")
    out, i = [], 0
    while i < len(body):
        t, v, i = _der(body, i)
        out.append((t, v))
    return out


def _pkcs1(der: bytes) -> _RsaKey:
    items = _der_seq(der)
    ints = [int.from_bytes(v, "big") for t, v in items if t == 0x02]
    if len(ints) < 4:
        raise SignatureError("the signing key is not a PKCS#1 RSA private key")
    return _RsaKey(n=ints[1], e=ints[2], d=ints[3])


def _load_private(path: Path) -> _RsaKey | str:
    """The RSA key, or the key type's name when it is another type (ssh-keygen may sign with it)."""
    try:
        text = path.read_text(encoding="ascii")
    except (OSError, UnicodeDecodeError) as e:
        raise SignatureError("the signing key is unreadable (%s)" % (getattr(e, "strerror", None) or type(e).__name__)) \
            from None
    der = _pem_body(text, "RSA PRIVATE KEY")
    if der is not None:
        return _pkcs1(der)
    der = _pem_body(text, "PRIVATE KEY")
    if der is not None:
        items = _der_seq(der)
        if len(items) < 3 or items[2][0] != 0x04 or b"\x2a\x86\x48\x86\xf7\x0d\x01\x01\x01" not in items[1][1]:
            return "pkcs8-non-rsa"
        return _pkcs1(items[2][1])
    raw = _pem_body(text, "OPENSSH PRIVATE KEY")
    if raw is None:
        raise SignatureError("the signing key is not a PEM or OpenSSH private key")
    if not raw.startswith(b"openssh-key-v1\x00"):
        raise SignatureError("the signing key is not an openssh-key-v1 key")
    r = _Reader(raw[len(b"openssh-key-v1\x00"):])
    cipher = r.string()
    r.string(), r.string()                                       # kdf name and options (none for cipher none)
    if cipher != b"none":
        raise SignatureError("the signing key is encrypted; the publish job never prompts for a passphrase")
    if r.u32() != 1:
        raise SignatureError("the signing key file holds more than one key")
    r.string()                                                    # public key
    p = _Reader(r.string())
    if p.u32() != p.u32():
        raise SignatureError("the signing key's check bytes differ (corrupt or encrypted)")
    kt = p.string().decode("ascii", "replace")
    if kt != "ssh-rsa":
        return kt
    n, e, d = p.mpint(), p.mpint(), p.mpint()
    return _RsaKey(n=n, e=e, d=d)


# ---------------------------------------------------------------------------------------------------------
# RSA PKCS#1 v1.5 (RFC 8017 section 8.2) over SSHSIG's signed data (PROTOCOL.sshsig)
# ---------------------------------------------------------------------------------------------------------
def _emsa(data: bytes, alg: str, k: int) -> bytes:
    t = _DIGEST_INFO[alg] + hashlib.new(alg, data).digest()
    if k < len(t) + 11:
        raise SignatureError("the RSA key is too short")
    return b"\x00\x01" + b"\xff" * (k - len(t) - 3) + b"\x00" + t


def _signed_data(data: bytes, namespace: str, hash_alg: str) -> bytes:
    return (MAGIC + _string(namespace.encode()) + _string(b"") + _string(hash_alg.encode())
            + _string(hashlib.new(hash_alg, data).digest()))


def _armor(blob: bytes) -> str:
    b64 = base64.b64encode(blob).decode()
    return "%s\n%s\n%s\n" % (ARMOR_BEGIN, "\n".join(b64[i:i + 70] for i in range(0, len(b64), 70)), ARMOR_END)


def _unarmor(text: str) -> bytes:
    m = re.search(r"%s\s*(.*?)\s*%s" % (re.escape(ARMOR_BEGIN), re.escape(ARMOR_END)), text or "", re.S)
    if not m:
        raise SignatureError("not an SSH signature (no armor)")
    try:
        return base64.b64decode("".join(m.group(1).split()), validate=True)
    except ValueError:
        raise SignatureError("the SSH signature is not valid base64") from None


def _builtin_sign(data: bytes, key: _RsaKey, namespace: str) -> str:
    k = (key.n.bit_length() + 7) // 8
    em = _emsa(_signed_data(data, namespace, "sha512"), "sha512", k)
    s = pow(int.from_bytes(em, "big"), key.d, key.n).to_bytes(k, "big")
    if pow(int.from_bytes(s, "big"), key.e, key.n).to_bytes(k, "big") != em:
        raise SignatureError("the RSA signature does not check against its own key (corrupt key)")
    blob = (MAGIC + _u32(1) + _string(key.blob) + _string(namespace.encode()) + _string(b"") + _string(b"sha512")
            + _string(_string(b"rsa-sha2-512") + _string(s)))
    return _armor(blob)


def _parse(armored: str) -> dict:
    r = _Reader(_unarmor(armored))
    if r.take(6) != MAGIC:
        raise SignatureError("not an SSHSIG signature")
    version = r.u32()
    if version != 1:
        raise SignatureError("SSHSIG version %d is not 1" % version)
    out = {"publickey": r.string(), "namespace": r.string().decode("utf-8", "replace"), "reserved": r.string(),
           "hash": r.string().decode("ascii", "replace"), "signature": r.string()}
    if not r.done():
        raise SignatureError("trailing bytes after the SSHSIG signature")
    return out


def _builtin_verify(data: bytes, sig: dict) -> None:
    pk = _Reader(sig["publickey"])
    if pk.string() != b"ssh-rsa":
        raise SignatureError("not an RSA signature: check it with ssh-keygen -Y verify")
    e, n = pk.mpint(), pk.mpint()
    sr = _Reader(sig["signature"])
    alg = sr.string().decode("ascii", "replace")
    s = sr.string()
    if alg not in _RSA_SIG:
        raise SignatureError("signature algorithm %s is not rsa-sha2-512 or rsa-sha2-256" % alg[:40])
    k = (n.bit_length() + 7) // 8
    if len(s) != k or int.from_bytes(s, "big") >= n:
        raise SignatureError("the RSA signature has the wrong length")
    if sig["hash"] not in ("sha512", "sha256"):
        raise SignatureError("SSHSIG hash %s is not sha512 or sha256" % sig["hash"][:20])
    want = _emsa(_signed_data(data, sig["namespace"], sig["hash"]), _RSA_SIG[alg], k)
    if pow(int.from_bytes(s, "big"), e, n).to_bytes(k, "big") != want:
        raise SignatureError("the signature does not match the file")


# ---------------------------------------------------------------------------------------------------------
# ssh-keygen (OpenSSH 8.1 and later)
# ---------------------------------------------------------------------------------------------------------
def _run(argv: list[str], stdin: bytes | None = None) -> subprocess.CompletedProcess:
    """The one subprocess call of this module (WRITE_CALLS: `local`): the configured ssh-keygen with -Y."""
    if len(argv) < 3 or argv[1] != "-Y" or argv[2] not in ("sign", "verify"):
        raise SignatureError("refused: only `ssh-keygen -Y sign|verify` runs here")
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "LC_ALL": "C"}
    return subprocess.run(argv, input=stdin if stdin is not None else b"", capture_output=True,
                          timeout=TIMEOUT_S, env=env, check=False)


@functools.lru_cache(maxsize=8)
def ssh_keygen_supports_y(ssh_keygen: str) -> bool:
    """Does this ssh-keygen know -Y (OpenSSH >= 8.1)? 8.0 answers `unknown option -- Y`."""
    try:
        r = _run([ssh_keygen, "-Y", "sign"])
    except (OSError, subprocess.SubprocessError):
        return False
    return b"unknown option" not in r.stderr and b"illegal option" not in r.stderr


def backend(key_path: str | os.PathLike | None, ssh_keygen: str = "ssh-keygen") -> tuple[str | None, str]:
    """('ssh-keygen' | 'builtin' | None, why). None means this host cannot sign with this key."""
    if not key_path:
        return None, "no signing key is configured ([stores.artifactory] signing_key)"
    p = Path(key_path)
    if not p.is_file():
        return None, "the signing key file does not exist"
    if ssh_keygen_supports_y(ssh_keygen):
        return "ssh-keygen", "ssh-keygen -Y sign"
    try:
        key = _load_private(p)
    except SignatureError as e:
        return None, str(e)
    if isinstance(key, _RsaKey):
        return "builtin", "built-in SSHSIG rsa-sha2-512 (this host's ssh-keygen has no -Y)"
    return None, ("the signing key is %s, and this host's ssh-keygen has no -Y (OpenSSH 8.0): use an RSA key "
                  "or set [stores.artifactory] ssh_keygen to an OpenSSH 8.1+ binary" % key)


def public_blob(key_path: str | os.PathLike) -> bytes:
    """The SSH wire public key of an RSA private key (the builtin signer's)."""
    key = _load_private(Path(key_path))
    if not isinstance(key, _RsaKey):
        raise SignatureError("not an RSA key")
    return key.blob


def sign(data: bytes, key_path: str | os.PathLike, *, ssh_keygen: str = "ssh-keygen",
         namespace: str = NAMESPACE) -> Signature:
    which, why = backend(key_path, ssh_keygen)
    if which is None:
        raise SignatureError(why)
    if which == "builtin":
        key = _load_private(Path(key_path))
        assert isinstance(key, _RsaKey)
        text = _builtin_sign(data, key, namespace)
        return Signature(text, "builtin", fingerprint(key.blob))
    with tempfile.TemporaryDirectory(prefix="rm-sign-") as tmp:
        f = Path(tmp) / "manifest"
        f.write_bytes(data)
        r = _run([ssh_keygen, "-Y", "sign", "-f", str(key_path), "-n", namespace, str(f)])
        sig = Path(str(f) + ".sig")
        if r.returncode != 0 or not sig.is_file():
            raise SignatureError("ssh-keygen -Y sign failed (exit %s)" % r.returncode)
        text = sig.read_text(encoding="ascii")
    return Signature(text, "ssh-keygen", fingerprint(_parse(text)["publickey"]))


def load_allowed(path: str | os.PathLike, principal: str = NAMESPACE) -> list[AllowedKey]:
    """The keys an allowed_signers file accepts (`principals [options] keytype base64 [comment]`), or the one key
    of a `.pub` file (`keytype base64 [comment]`, accepted for `principal`)."""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as e:
        raise SignatureError("the allowed signers file is unreadable (%s)" % type(e).__name__) from None
    out = []
    for ln in text.splitlines():
        ln = ln.strip()
        if not ln or ln.startswith("#"):
            continue
        toks = re.findall(r'(?:[^\s"]|"[^"]*")+', ln)
        at = next((i for i, t in enumerate(toks) if re.match(r"^(ssh-|ecdsa-|sk-)", t)), None)
        if at is None or at + 1 >= len(toks):
            continue
        try:
            blob = base64.b64decode(toks[at + 1], validate=True)
        except ValueError:
            continue
        names, ns = (principal,), None
        if at > 0:
            names = tuple(toks[0].split(","))
            m = next((re.match(r'^namespaces="([^"]*)"$', o) for o in ",".join(toks[1:at]).split(",")
                      if o.startswith("namespaces=")), None)
            ns = tuple(m.group(1).split(",")) if m else None
        for n in names:
            out.append(AllowedKey(n, blob, ns))
    if not out:
        raise SignatureError("the allowed signers file names no key")
    return out


def verify(data: bytes, armored: str, allowed: Iterable[AllowedKey], *, namespace: str = NAMESPACE,
           principal: str | None = None, ssh_keygen: str = "ssh-keygen") -> str:
    """The signer's fingerprint when `armored` signs `data` in `namespace` with an allowed key (for `principal`,
    when given); SignatureError otherwise."""
    sig = _parse(armored)
    if sig["namespace"] != namespace:
        raise SignatureError("the signature is for namespace %r, not %r" % (sig["namespace"][:40], namespace))
    keys = [a for a in allowed if a.blob == sig["publickey"] and (principal is None or a.principal == principal)
            and (a.namespaces is None or namespace in a.namespaces)]
    if not keys:
        raise SignatureError("the signing key %s is not an allowed signer" % fingerprint(sig["publickey"]))
    if _Reader(sig["publickey"]).string() == b"ssh-rsa":
        _builtin_verify(data, sig)
        return fingerprint(sig["publickey"])
    if not ssh_keygen_supports_y(ssh_keygen):
        raise SignatureError("a %s signature needs ssh-keygen -Y verify (OpenSSH 8.1+), not on this host"
                             % keys[0].keytype)
    with tempfile.TemporaryDirectory(prefix="rm-verify-") as tmp:
        (Path(tmp) / "allowed").write_text(keys[0].line())
        (Path(tmp) / "sig").write_text(armored)
        r = _run([ssh_keygen, "-Y", "verify", "-f", str(Path(tmp) / "allowed"), "-I", keys[0].principal,
                  "-n", namespace, "-s", str(Path(tmp) / "sig")], stdin=data)
    if r.returncode != 0:
        raise SignatureError("ssh-keygen -Y verify refused the signature")
    return fingerprint(sig["publickey"])
