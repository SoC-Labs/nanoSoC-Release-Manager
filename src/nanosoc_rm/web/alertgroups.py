"""Alerts grouped by cause (Wave C spec 5.1, build-first fix 1; CR-W3). Pure reads of the stored `alert` rows.

Fifty `mirror_failed` alerts with one message are one problem, not fifty: the dashboard, the rail, the inbox and
`/alerts` show one line per cause.

    normalise(message) -> str                 the message with URLs, quoted text, host:path keys and commit ids
                                              taken out, so the same cause on different repos reads the same
    cause_of(alert) -> (key, label, advice)   the first CAUSES row whose kind and pattern match; an unmatched
                                              message forms its own group, keyed by a hash of its normalised text
    groups(config, alerts=None) -> [group]    open alerts by cause, biggest group first:
        {key, label, advice, kind, count, by_host: {host: n}, hosts_text, subjects[], newest_at, sample[3],
         alerts[], fp}                        fp = seen_fp(subjects): what a clear-group POST must match
    group(config, key) -> group | None

No hostname reaches a label: an unmatched message's label is its normalised text, which has no URL left in it,
and a page passes every message through `shell.redact()` before showing it.
"""
from __future__ import annotations

import hashlib
import re
from typing import Any, Iterable

from nanosoc_rm import clock, db

__all__ = ["CAUSES", "normalise", "cause_of", "groups", "group", "host_of", "seen_fp"]

# (alert_kind or None for any kind, pattern on the normalised message, key, label, advice). First match wins.
CAUSES: tuple[tuple[str | None, str, str, str, str], ...] = (
    # CR-PR1: a failure WITH the host's private_read method is its own cause, before the anonymous ones.
    ("mirror_failed", r"failed with read credentials",
     "private-creds", "private repos the read credentials cannot read",
     "Check that the host's private_read key or token can read these repos, then sync again"),
    ("mirror_failed", r"private_read is misconfigured",
     "private-read-config", "mirrors whose private_read setting cannot be used",
     "Fix [hosts.<host>] private_read in the service config (docs/DEPLOY.md)"),
    ("mirror_failed", r"could not read (username|password).*terminal prompts disabled|authentication failed"
                      r"|terminal prompts disabled",
     "private-anon", "private repos cannot be mirrored anonymously",
     "Set private_read for the host (one config line, docs/DEPLOY.md), or mark these repos private so the "
     "mirror skips them"),
    ("mirror_failed", r"permission denied \(publickey\)|host key verification failed",
     "ssh-key", "repos the mirror reads over ssh without a key",
     "Mirror them over https, or give the service a read-only deploy key"),
    ("mirror_failed", r"repository .*not found|not found|does not exist|does not appear to be a git repository",
     "repo-gone", "repos no longer found on their host",
     "Check the catalogue: the repo was renamed, moved or deleted"),
    ("mirror_failed", r"timed out|could not resolve host|connection (refused|reset)|unable to access|network",
     "unreachable", "repos whose host could not be reached",
     "Check the network from this host, then run a mirror sync again"),
    ("rewrite", r"", "rewrite", "branches moved without descent (history rewritten)",
     "Check each rewritten branch: a pinned or cited commit may be gone"),
    ("tag_moved", r"", "tag-moved", "tags that moved to another commit",
     "Find who moved each tag; a release tag must never move"),
    ("waiver_unused", r"", "waiver-unused", "runs where a T0 waiver covered nothing",
     "Remove the unused waivers from eligibility.yaml once their cause is fixed"),
    ("token_expiry", r"", "token-expiry", "credentials that expire soon",
     "Renew the token before it expires (nanosoc-rm credentials check)"),
    ("runner_drift", r"", "runner-drift", "runners that drifted from their hardened settings",
     "Restore the runner settings, then check them again"),
    ("unknown_url", r"", "unknown-url", "pins whose URL is not in the catalogue",
     "Add the repo to the catalogue, or fix the pin's URL"),
)
_COMPILED = tuple((k, re.compile(p, re.I | re.S), key, label, advice) for k, p, key, label, advice in CAUSES)
SAMPLE = 3

_URL = re.compile(r"(?:https?|ssh|git|file)://[^\s'\"<>)]+|[A-Za-z0-9._-]+@[A-Za-z0-9.-]+:[^\s'\"<>)]+")
_QUOTED = re.compile(r"'[^']*'|\"[^\"]*\"")
_KEY = re.compile(r"\b[A-Za-z0-9_.-]+:[A-Za-z0-9_.][A-Za-z0-9_./@-]*")
_SHA = re.compile(r"\b[0-9a-f]{7,40}\b")
_NUM = re.compile(r"\b\d+\b")
_SPACE = re.compile(r"\s+")


def normalise(message: Any) -> str:
    """The message with what differs between alerts of one cause removed (URLs, quoted text, host:path keys,
    commit ids, counts), whitespace collapsed."""
    s = _URL.sub("<url>", str(message or ""))
    s = _QUOTED.sub("''", s)
    s = _KEY.sub("<repo>", s)
    s = _SHA.sub("<sha>", s)
    s = _NUM.sub("<n>", s)
    return _SPACE.sub(" ", s).strip()


def cause_of(alert: dict) -> tuple[str, str, str]:
    """(key, label, advice) of one alert observation."""
    f = alert.get("fact") or {}
    kind = str(f.get("alert_kind") or "")
    text = normalise(f.get("message"))
    for k, rx, key, label, advice in _COMPILED:
        if (k is None or k == kind) and rx.search(text):
            return key, label, advice
    digest = hashlib.sha256(("%s\n%s" % (kind, text)).encode()).hexdigest()[:12]
    label = "%s: %s" % (kind or "alert", text[:140] or "(no message)")
    return "x-%s" % digest, label, "Read the alerts in this group; each one says what failed"


def _host_names(config) -> list[str]:
    try:
        return [str(h) for h in (config.get("hosts", {}) or {})]
    except Exception:                                            # noqa: BLE001 - no host table: no split
        return []


def host_of(alert: dict, hosts: Iterable[str]) -> str | None:
    """The configured host an alert is about (`mirror_status:gitlab:...` -> gitlab), or None."""
    hosts = list(hosts)
    for part in str((alert.get("fact") or {}).get("about") or "").split(":")[:3]:
        if part in hosts:
            return part
    return None


def groups(config, alerts: list[dict] | None = None) -> list[dict]:
    """Open alerts grouped by cause, the biggest group first (then the newest)."""
    docs = db.alerts(open_only=True, config=config) if alerts is None else [
        a for a in alerts if not (a.get("fact") or {}).get("cleared_at")]
    hosts = _host_names(config)
    out: dict[str, dict] = {}
    for a in docs:
        key, label, advice = cause_of(a)
        g = out.setdefault(key, {"key": key, "label": label, "advice": advice,
                                 "kind": (a.get("fact") or {}).get("alert_kind"), "count": 0, "by_host": {},
                                 "subjects": [], "newest_at": None, "alerts": []})
        g["count"] += 1
        g["subjects"].append(a["subject"])
        g["alerts"].append(a)
        h = host_of(a, hosts)
        if h is not None:
            g["by_host"][h] = g["by_host"].get(h, 0) + 1
        at = (a.get("fact") or {}).get("raised_at") or a.get("observed_at")
        if at and (g["newest_at"] is None or clock.parse(at) > clock.parse(g["newest_at"])):
            g["newest_at"] = at
    for g in out.values():
        g["alerts"].sort(key=lambda a: clock.parse((a.get("fact") or {}).get("raised_at") or a["observed_at"]),
                         reverse=True)
        g["sample"] = g["alerts"][:SAMPLE]
        g["by_host"] = dict(sorted(g["by_host"].items(), key=lambda kv: (-kv[1], kv[0])))
        g["hosts_text"] = ", ".join("%d %s" % (n, h) for h, n in g["by_host"].items())
        g["fp"] = seen_fp(g["subjects"])
    return sorted(out.values(), key=lambda g: (-g["count"], -clock.parse(g["newest_at"]).timestamp()
                                               if g["newest_at"] else 0, g["key"]))


def seen_fp(subjects: Iterable[str]) -> str:
    """The fingerprint of the alerts a page showed in one group: a clear-group POST carries it, and the server
    clears only when the group still holds exactly those alerts (I-R3)."""
    return hashlib.sha256("\n".join(sorted(subjects)).encode()).hexdigest()[:24]


def group(config, key: str) -> dict | None:
    return next((g for g in groups(config) if g["key"] == key), None)
