"""The generated-RTL archive (G7): an md5 manifest of every regular file in a tar archive of build_soc/.

    manifest(archive) -> Manifest     md5 of the archive, one `<md5>  <path>` line per regular file, sorted
    compare(manifest_text, root) -> [differences]   a build_soc/ directory against a manifest, file for file

The archive is read, never extracted: member contents are hashed from the tar stream. A member with an absolute
path or a `..` part is refused (ArchiveError), so a manifest can never name a file outside its tree. Symlinks and
directories are counted but not listed (the manifest lists regular files, as `md5sum` over `find -type f` does).
The format matches `md5sum` (two spaces), sorted by path in byte order.
"""
from __future__ import annotations

import hashlib
import os
import tarfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from nanosoc_rm.releases.core import md5_bytes, md5_file

__all__ = ["Manifest", "ArchiveError", "manifest", "compare", "parse_manifest"]


class ArchiveError(ValueError):
    """Not a readable tar archive, or a member path that escapes the archive's tree."""


@dataclass
class Manifest:
    md5: str
    bytes: int
    files: int
    symlinks: int
    text: str
    manifest_md5: str
    members: list[tuple[str, bytes]] = field(default_factory=list, repr=False)   # (path, content) for the scan


def _safe(name: str) -> str:
    p = PurePosixPath(name)
    if p.is_absolute() or ".." in p.parts:
        raise ArchiveError("archive member %r escapes the archive's tree" % name)
    return str(p)


def manifest(archive: str | os.PathLike, keep_members: bool = False) -> Manifest:
    """The md5 manifest of a tar archive (any compression tarfile reads). `keep_members` keeps each regular
    file's content (for the vendor scan, D11)."""
    path = Path(archive)
    digest, size = md5_file(path)
    lines, symlinks, members = [], 0, []
    try:
        with tarfile.open(path, "r:*") as tar:
            for m in tar:
                name = _safe(m.name)
                if m.issym() or m.islnk():
                    symlinks += 1
                    continue
                if not m.isfile():
                    continue
                fh = tar.extractfile(m)
                h = hashlib.md5()
                data = bytearray() if keep_members else None
                while True:
                    b = fh.read(1 << 20)
                    if not b:
                        break
                    h.update(b)
                    if data is not None:
                        data += b
                lines.append((name, h.hexdigest()))
                if data is not None:
                    members.append((name, bytes(data)))
    except (tarfile.TarError, OSError, EOFError) as e:
        raise ArchiveError("%s is not a readable tar archive: %s" % (path.name, e)) from None
    lines.sort(key=lambda x: x[0].encode())
    text = "".join("%s  %s\n" % (h, n) for n, h in lines)
    return Manifest(digest, size, len(lines), symlinks, text, md5_bytes(text.encode()), members)


def parse_manifest(text: str) -> dict[str, str]:
    out = {}
    for line in text.splitlines():
        if line.strip():
            h, _, name = line.partition("  ")
            out[name] = h
    return out


def compare(manifest_text: str, root: str | os.PathLike) -> list[str]:
    """G7 done-when: does `root` (a build_soc/ checkout's PARENT, or the directory the archive paths start in)
    hold exactly the manifest's files with the same md5? One line per difference; [] = file for file."""
    root = Path(root)
    want = parse_manifest(manifest_text)
    tops = {n.split("/", 1)[0] for n in want}
    have: dict[str, str] = {}
    for top in tops:
        base = root / top
        if not base.is_dir():
            continue
        for dirpath, _, files in os.walk(base):
            for f in files:
                p = Path(dirpath) / f
                if p.is_symlink() or not p.is_file():
                    continue
                have[str(p.relative_to(root))] = md5_file(p)[0]
    out = ["missing: %s" % n for n in sorted(set(want) - set(have))]
    out += ["extra: %s" % n for n in sorted(set(have) - set(want))]
    out += ["differs: %s" % n for n in sorted(set(want) & set(have)) if want[n] != have[n]]
    return out
