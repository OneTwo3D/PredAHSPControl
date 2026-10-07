"""Time arithmetic that is correct across daylight-saving changes.

Python subtracts and adds aware datetimes that share one ``tzinfo`` (e.g. ``ZoneInfo("Europe/London")``)
in wall-clock time: across the autumn change 01:55 BST → 01:00 GMT is −55 min, and adding hours skips
or repeats the shifted hour. These helpers do the arithmetic in UTC and convert back to the original
zone, so local fields (hour of day, date) stay available for schedules and display. Naive datetimes are
treated as plain wall-clock values.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta

_HOUR = timedelta(hours=1)


def add_hours(t: datetime, hours: float) -> datetime:
    """``t`` plus elapsed ``hours``, expressed in ``t``'s zone."""
    if t.tzinfo is None:
        return t + timedelta(hours=hours)
    return (t.astimezone(UTC) + timedelta(hours=hours)).astimezone(t.tzinfo)


def elapsed_s(a: datetime, b: datetime) -> float:
    """Elapsed seconds from ``a`` to ``b``."""
    if a.tzinfo is None or b.tzinfo is None:
        return (b - a).total_seconds()
    return (b.astimezone(UTC) - a.astimezone(UTC)).total_seconds()


def elapsed_h(a: datetime, b: datetime) -> float:
    return elapsed_s(a, b) / 3600.0


def hour_key(t: datetime) -> datetime:
    """Start of the elapsed hour containing ``t``, in UTC so the repeated autumn hour gets its own key."""
    if t.tzinfo is None:
        return t.replace(minute=0, second=0, microsecond=0)
    return t.astimezone(UTC).replace(minute=0, second=0, microsecond=0)


def day_length_h(d: date, tz: object) -> float:
    """Length of local date ``d`` in hours (23 or 25 on daylight-saving change days)."""
    if tz is None:
        return 24.0
    a = datetime.combine(d, time(), tzinfo=tz)  # type: ignore[arg-type]
    b = datetime.combine(d + timedelta(days=1), time(), tzinfo=tz)  # type: ignore[arg-type]
    return elapsed_h(a, b)
