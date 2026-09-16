from datetime import UTC, datetime

from macos_bridge.time_utils import mac_absolute_to_iso, mac_absolute_to_unix


def test_seconds_format_round_trip():
    # 2024-01-01T00:00:00 UTC = unix 1704067200 = mac 1704067200 - 978307200 = 725760000.
    iso = mac_absolute_to_iso(725760000)
    assert iso == "2024-01-01T00:00:00+00:00"


def test_nanoseconds_format_round_trip():
    iso = mac_absolute_to_iso(725760000 * 1_000_000_000)
    assert iso == "2024-01-01T00:00:00+00:00"


def test_zero_returns_none():
    assert mac_absolute_to_iso(0) is None


def test_unix_conversion_matches_datetime():
    dt = datetime(2024, 6, 15, 12, 0, 0, tzinfo=UTC)
    mac_seconds = int(dt.timestamp() - 978307200)
    unix = mac_absolute_to_unix(mac_seconds)
    assert datetime.fromtimestamp(unix, tz=UTC) == dt
