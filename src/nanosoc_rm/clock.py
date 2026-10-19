"""UTC timestamps in the contract format (defs.schema.json#/$defs/utc: RFC 3339 with a Z suffix)."""
from __future__ import annotations

import datetime as dt


def now() -> dt.datetime:
    """The current time, timezone-aware UTC, to the second."""
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0)


def iso(t: dt.datetime | None = None) -> str:
    """`t` (default: now) as `YYYY-MM-DDTHH:MM:SSZ`. A naive datetime is taken to be UTC."""
    t = now() if t is None else t
    if t.tzinfo is None:
        t = t.replace(tzinfo=dt.timezone.utc)
    return t.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse(s: str) -> dt.datetime:
    """A contract timestamp (Z suffix, optional fraction) as an aware UTC datetime. Anything but a string (a null
    time in an API answer) is a TypeError, never an AttributeError."""
    if not isinstance(s, str):
        raise TypeError("a timestamp must be a string, not %s" % type(s).__name__)
    t = dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
    if t.tzinfo is None:
        t = t.replace(tzinfo=dt.timezone.utc)
    return t.astimezone(dt.timezone.utc)
