"""The F5 import census (interfaces.md section 2a; import-ledger schema `items_rule`).

    census(ledger, read_doc) -> [CensusRow]      read_doc(component, commit, path) -> the document's text

For each source of `imports.yaml`: apply `items_rule.pattern` (a Python regex, multiline, first group = the item
key) to the document at the cited commit, drop the `exclude` keys, add the `include` keys. Then

    missing  censused keys the ledger lacks
    extra    ledger keys the census lacks
    pending  ledger items still `pending`
    done     a rule is present, nothing missing, nothing extra, nothing pending (and the document was read)

A source without a rule, with a rule that does not compile or has no group, or whose document cannot be read
is not done, and `error` says why (a field after the contracted ones, default None). A ConfigError from the
reader (the component's host is not configured) is not a property of the source: it leaves census, so the CLI
exits 3 (section 0.2).

`read_doc` for the CLI (`git_reader`) reads the document at the cited commit from the component's mirror
(`<data_dir>/mirrors/<component>.git`) when it has the commit, else by an anonymous shallow fetch of that one
commit from the component's canonical repo into a throwaway repository. It never writes to a host.
"""
from __future__ import annotations

import re
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable

from nanosoc_rm import gitrun
from nanosoc_rm.config import ConfigError

__all__ = ["CensusRow", "census", "census_source", "git_reader", "ReadError"]

ReadDoc = Callable[[str, str, str], str]


@dataclass
class CensusRow:
    path: str
    commit: str
    census: list[str]
    missing: list[str]
    extra: list[str]
    excluded: list[dict]
    pending: int
    done: bool
    component: str | None = None
    error: str | None = None

    def as_doc(self) -> dict:
        return asdict(self)


class ReadError(RuntimeError):
    """A source document could not be read at its cited commit."""


def _unique(seq: list[str]) -> list[str]:
    seen, out = set(), []
    for x in seq:
        if x not in seen:
            seen.add(x)
            out.append(x)
    return out


def census_source(source: dict, read_doc: ReadDoc) -> CensusRow:
    """The census of one ledger source."""
    doc = source["doc"]
    rule = doc.get("items_rule")
    items = source.get("items", [])
    listed = _unique([it["key"] for it in items])
    pending = sum(1 for it in items if it.get("disposition") == "pending")
    excluded = [dict(x) for x in (rule or {}).get("exclude", [])]
    row = CensusRow(path=doc["path"], commit=doc["commit"], census=[], missing=[], extra=list(listed),
                    excluded=excluded, pending=pending, done=False, component=doc.get("component"))
    if not rule:
        row.error = "no items_rule: the document cannot be censused"
        return row
    try:
        pattern = re.compile(rule["pattern"], re.MULTILINE)
    except re.error as e:
        row.error = "items_rule.pattern does not compile: %s" % e
        return row
    if pattern.groups < 1:
        row.error = "items_rule.pattern has no group: the first group is the item key"
        return row
    try:
        text = read_doc(doc["component"], doc["commit"], doc["path"])
    except ConfigError:
        raise
    except Exception as e:                     # any reader failure: this source is not censused
        row.error = "cannot read %s at %s: %s" % (doc["path"], doc["commit"][:12], e)
        return row
    drop = {x["key"] for x in excluded}
    keys = [m.group(1) for m in pattern.finditer(text) if m.group(1) is not None]
    keys = [k for k in keys if k not in drop] + [x["key"] for x in rule.get("include", [])]
    row.census = _unique(keys)
    row.missing = [k for k in row.census if k not in listed]
    row.extra = [k for k in listed if k not in row.census]
    row.done = not row.missing and not row.extra and pending == 0
    return row


def census(ledger: dict, read_doc: ReadDoc) -> list[CensusRow]:
    """One CensusRow per source of the ledger, in ledger order."""
    return [census_source(s, read_doc) for s in ledger.get("sources", [])]


# ---------------------------------------------------------------------------------------------------------
# A reader for the CLI: the mirror first, else an anonymous fetch of exactly the cited commit
# ---------------------------------------------------------------------------------------------------------
def _show(git_dir: Path, commit: str, path: str) -> str | None:
    have = gitrun.run_git("--git-dir", git_dir, "cat-file", "-e", "%s^{commit}" % commit, timeout=60)
    if not have.ok:
        return None
    blob = gitrun.run_git("--git-dir", git_dir, "show", "%s:%s" % (commit, path), timeout=300)
    if not blob.ok:
        tail = (blob.stderr or "").strip().splitlines()[-1:] or ["exit %s" % blob.returncode]
        raise ReadError("%s at %s is not readable from %s: %s" % (path, commit[:12], git_dir.name, tail[0]))
    return blob.stdout


def git_reader(registry: Any, config: Any) -> ReadDoc:
    """read_doc(component, commit, path) over the service's mirrors and the canonical hosts' git bases
    (`hosts.<host>.git` from the config)."""
    def read(component: str, commit: str, path: str) -> str:
        mirror = config.mirrors_dir / ("%s.git" % component)
        if mirror.is_dir():
            text = _show(mirror, commit, path)
            if text is not None:
                return text
        repo = registry.canonical(component)
        base = str(config.host(repo.host).get("git") or "").rstrip("/")
        if not base:
            raise ReadError("hosts.%s has no web or git base to fetch from" % repo.host)
        url = "%s/%s.git" % (base, repo.path)
        with tempfile.TemporaryDirectory(prefix="nanosoc-rm-census-") as tmp:
            gitrun.run_git("init", "--quiet", "--bare", tmp, timeout=60).check()
            fetch = gitrun.run_git("--git-dir", tmp, "fetch", "--quiet", "--no-tags", "--depth=1", url, commit,
                                   timeout=300)
            if not fetch.ok:
                tail = (fetch.stderr or "").strip().splitlines()[-1:] or ["exit %s" % fetch.returncode]
                raise ReadError("%s:%s is not fetchable from %s: %s" % (repo.host, repo.path, commit[:12], tail[0]))
            text = _show(Path(tmp), commit, path)
            if text is None:
                raise ReadError("the fetch of %s did not bring commit %s" % (repo.path, commit[:12]))
            return text
    return read

