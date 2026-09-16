"""ISO-8601 UTC timestamp formatting.

All three bridges format the same way: UTC, millisecond precision,
``Z`` suffix instead of ``+00:00``, empty string for missing/zero
values so HA's ``device_class: timestamp`` sensors render as ``Unknown``
instead of ``1970-01-01T00:00:00.000Z``.
"""

from __future__ import annotations

import datetime
import math
import time


def _format(dt: datetime.datetime) -> str:
    """Render a tz-aware datetime as ``YYYY-MM-DDTHH:MM:SS.fffZ``.

    Forces millisecond precision regardless of input (Python's default
    ``isoformat()`` keeps full microseconds, which HA renders fine but
    the bridges have always emitted milliseconds for consistency with
    JavaScript clients).
    """
    # Strip to millisecond resolution.
    dt = dt.astimezone(datetime.timezone.utc).replace(
        microsecond=(dt.microsecond // 1000) * 1000
    )
    return dt.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def epoch_to_iso(seconds: float | int | None) -> str:
    """Convert epoch seconds to ISO-8601 UTC.

    Returns ``""`` for ``None``, non-numeric, or non-positive values so
    HA's ``timestamp`` device class renders the entity as ``Unknown``
    rather than the Unix epoch.
    """
    if seconds is None:
        return ""
    try:
        v = float(seconds)
    except (TypeError, ValueError):
        return ""
    if not math.isfinite(v) or v <= 0:
        return ""
    return _format(datetime.datetime.fromtimestamp(v, tz=datetime.timezone.utc))


def epoch_ms_to_iso(ms: int | str | None) -> str:
    """Convert epoch milliseconds (or numeric string) to ISO-8601 UTC.

    Mirrors :func:`epoch_to_iso` but for the millisecond timebase
    several APIs use (Govee cloud, JavaScript clients).
    """
    if ms is None:
        return ""
    try:
        v = int(ms)
    except (TypeError, ValueError):
        return ""
    if v <= 0:
        return ""
    return _format(
        datetime.datetime.fromtimestamp(v / 1000, tz=datetime.timezone.utc)
    )


def iso_now() -> str:
    """Current UTC time as ISO-8601, millisecond precision, ``Z`` suffix."""
    return _format(datetime.datetime.now(tz=datetime.timezone.utc))


def now_ms() -> int:
    """Current wall-clock time as epoch milliseconds."""
    return int(time.time() * 1000)


def now_s() -> int:
    """Current wall-clock time as epoch seconds."""
    return int(time.time())


def iso_from_monotonic(start_monotonic: float) -> str:
    """ISO-8601 timestamp for ``time.time() - (time.monotonic() -
    start_monotonic)``.

    Convenience for converting a captured ``time.monotonic()`` reading
    into a wall-clock ISO string — used by phase tickers that timestamp
    when the underlying probe finished rather than when the publish
    happens.
    """
    delta = time.monotonic() - start_monotonic
    wall = time.time() - delta
    return epoch_to_iso(wall)
