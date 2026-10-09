"""Time-bound parsing for ChatGPT period selection.

`--since` / `--until` accept ISO dates (`2026-08-01`), full ISO
timestamps, or relative durations (`7d`, `14d`, `30d`).
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Optional, Union


_RELATIVE_RE = re.compile(r"^(\d+)\s*([dDhHmM])$")


class TimeBoundError(ValueError):
    """Invalid since/until / time-field value."""


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def parse_remote_time(value: object) -> Optional[datetime]:
    """Normalize ChatGPT create_time / update_time to aware UTC.

    Accepts unix seconds (int/float), ISO-8601 strings, or datetime.
    """
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(float(value), tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(text)
        except ValueError:
            try:
                return datetime.fromtimestamp(float(text), tz=timezone.utc)
            except (ValueError, OverflowError, OSError):
                return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    return None


def to_iso_utc(value: object) -> Optional[str]:
    dt = parse_remote_time(value)
    if dt is None:
        return None
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_time_bound(
    value: Optional[str],
    *,
    now: Optional[datetime] = None,
    end_of_day: bool = False,
) -> Optional[datetime]:
    """Parse a CLI time bound.

    ``end_of_day=True`` (used for ``--until`` date-only values) extends
    a calendar date to 23:59:59.999999 UTC so the full day is included.
    Relative durations (`7d`) are always computed from ``now``.
    """
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    clock = now or utcnow()
    if clock.tzinfo is None:
        clock = clock.replace(tzinfo=timezone.utc)
    else:
        clock = clock.astimezone(timezone.utc)

    rel = _RELATIVE_RE.match(text)
    if rel:
        amount = int(rel.group(1))
        unit = rel.group(2).lower()
        if unit == "d":
            delta = timedelta(days=amount)
        elif unit == "h":
            delta = timedelta(hours=amount)
        else:
            delta = timedelta(minutes=amount)
        return clock - delta

    if re.fullmatch(r"\d{4}-\d{2}-\d{2}$", text):
        dt = datetime.strptime(text, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        if end_of_day:
            return dt.replace(hour=23, minute=59, second=59, microsecond=999999)
        return dt

    dt = parse_remote_time(text)
    if dt is None:
        raise TimeBoundError(
            f"invalid time bound {value!r}: expected YYYY-MM-DD, "
            f"ISO timestamp, or relative duration like 7d"
        )
    return dt


def in_range(
    value: object,
    *,
    since: Optional[datetime] = None,
    until: Optional[datetime] = None,
) -> bool:
    dt = parse_remote_time(value)
    if dt is None:
        return False
    if since is not None and dt < since:
        return False
    if until is not None and dt > until:
        return False
    return True


def resolve_time_field(name: Optional[str]) -> str:
    field = (name or "updated").strip().lower()
    if field in ("updated", "update", "update_time"):
        return "updated"
    if field in ("created", "create", "create_time"):
        return "created"
    raise TimeBoundError(
        f"invalid time-field {name!r}: expected 'updated' or 'created'"
    )
