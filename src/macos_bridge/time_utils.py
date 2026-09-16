"""Mac Absolute Time (CFAbsoluteTime) helpers.

Mac Absolute Time is seconds (or, in some Apple schemas, nanoseconds)
since 2001-01-01 00:00:00 UTC.

  - knowledgeC.db ZSTARTDATE / ZENDDATE: seconds (CFAbsoluteTime)
  - chat.db `message.date` (post-El Capitan): nanoseconds
  - CallHistory.storedata `ZDATE`: seconds (CoreData NSDate)
  - FaceTimeMessageStore-local.sqlitedb `ZDATECREATED`: seconds

The unix-conversion helper auto-detects unit from magnitude.
"""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

# Unix epoch seconds at 2001-01-01 00:00:00 UTC.
MAC_ABSOLUTE_TIME_EPOCH = 978307200.0
MAC_EPOCH_OFFSET = 978307200  # alias kept for back-compat with comms code paths


def datetime_to_mac_absolute_time(dt: datetime) -> float:
    if dt.tzinfo is None:
        raise ValueError("datetime must be timezone-aware")
    return dt.timestamp() - MAC_ABSOLUTE_TIME_EPOCH


def mac_absolute_time_to_datetime(mat: float) -> datetime:
    return datetime.fromtimestamp(mat + MAC_ABSOLUTE_TIME_EPOCH, tz=UTC)


def start_of_today_mac_absolute_time(tz: ZoneInfo | None = None) -> float:
    """Mac Absolute Time for the start of today in the given timezone.
    Defaults to the system local timezone. fold=0 is canonical to avoid
    DST-day ambiguity.
    """
    if tz is None:
        tz = datetime.now().astimezone().tzinfo
    today = datetime.now(tz=tz).date()
    start_of_day = datetime(today.year, today.month, today.day, tzinfo=tz)
    return datetime_to_mac_absolute_time(start_of_day)


def mac_absolute_to_unix(value: float | int | None) -> float:
    if value is None:
        return 0.0
    # Magnitudes above ~10^11 are nanoseconds.
    seconds = value / 1e9 if value > 1e11 else float(value)
    return seconds + MAC_EPOCH_OFFSET


def mac_absolute_to_iso(value: float | int | None) -> str | None:
    if value is None or value == 0:
        return None
    unix_ts = mac_absolute_to_unix(value)
    return datetime.fromtimestamp(unix_ts, tz=UTC).isoformat()


def unix_to_iso(unix_ts: float) -> str:
    return datetime.fromtimestamp(unix_ts, tz=UTC).isoformat()
