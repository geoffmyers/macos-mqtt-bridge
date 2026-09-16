from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from macos_bridge.time_utils import (
    MAC_ABSOLUTE_TIME_EPOCH,
    datetime_to_mac_absolute_time,
    mac_absolute_time_to_datetime,
    start_of_today_mac_absolute_time,
)


def test_epoch_constant():
    # 2001-01-01 00:00:00 UTC in Unix seconds
    assert MAC_ABSOLUTE_TIME_EPOCH == 978307200.0


def test_datetime_to_mac_absolute_time_at_epoch():
    dt = datetime(2001, 1, 1, 0, 0, 0, tzinfo=UTC)
    assert datetime_to_mac_absolute_time(dt) == 0.0


def test_round_trip_preserves_value():
    dt = datetime(2026, 4, 26, 12, 30, 45, tzinfo=UTC)
    mat = datetime_to_mac_absolute_time(dt)
    assert mac_absolute_time_to_datetime(mat) == dt


def test_start_of_today_is_midnight_local():
    tz = ZoneInfo("America/New_York")
    result_mat = start_of_today_mac_absolute_time(tz=tz)
    result_dt = mac_absolute_time_to_datetime(result_mat).astimezone(tz)
    assert result_dt.hour == 0
    assert result_dt.minute == 0
    assert result_dt.second == 0
    assert result_dt.microsecond == 0


def test_start_of_today_is_in_the_past_or_now():
    now_utc = datetime.now(UTC)
    result_mat = start_of_today_mac_absolute_time()
    result_dt = mac_absolute_time_to_datetime(result_mat)
    assert result_dt <= now_utc
    # And no more than 26 hours ago (DST safety)
    assert now_utc - result_dt < timedelta(hours=26)
