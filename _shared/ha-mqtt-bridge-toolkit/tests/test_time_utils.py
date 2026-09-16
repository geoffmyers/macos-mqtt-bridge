"""Tests for ha_mqtt_bridge.time_utils."""

from __future__ import annotations

import re

import pytest

from ha_mqtt_bridge.time_utils import (
    epoch_ms_to_iso,
    epoch_to_iso,
    iso_now,
    now_ms,
    now_s,
)

ISO_MS_Z = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$")


class TestEpochToIso:
    def test_known_value(self) -> None:
        # 2024-01-01T00:00:00Z
        assert epoch_to_iso(1704067200) == "2024-01-01T00:00:00.000Z"

    def test_subsecond(self) -> None:
        # 2024-01-01T00:00:00.500Z
        assert epoch_to_iso(1704067200.5) == "2024-01-01T00:00:00.500Z"

    @pytest.mark.parametrize("v", [None, 0, -1, "", "nan", "garbage"])
    def test_missing_returns_empty(self, v) -> None:
        assert epoch_to_iso(v) == ""

    def test_string_numeric_accepted(self) -> None:
        # float() accepts the string form, so this should succeed.
        assert epoch_to_iso("1704067200") == "2024-01-01T00:00:00.000Z"


class TestEpochMsToIso:
    def test_known_value(self) -> None:
        assert epoch_ms_to_iso(1704067200_000) == "2024-01-01T00:00:00.000Z"

    def test_string_input(self) -> None:
        # Govee returns lastTime as numeric, but the toolkit accepts the
        # string form as a convenience (matches the legacy
        # epoch_ms_to_iso shape).
        assert epoch_ms_to_iso("1704067200000") == "2024-01-01T00:00:00.000Z"

    @pytest.mark.parametrize("v", [None, 0, -1, "", "garbage", "1.5"])
    def test_missing_returns_empty(self, v) -> None:
        assert epoch_ms_to_iso(v) == ""

    def test_subsecond_ms(self) -> None:
        # 2024-01-01T00:00:00.123Z
        assert epoch_ms_to_iso(1704067200_123) == "2024-01-01T00:00:00.123Z"


class TestIsoNow:
    def test_shape(self) -> None:
        s = iso_now()
        assert ISO_MS_Z.match(s), s

    def test_changes(self) -> None:
        import time

        a = iso_now()
        time.sleep(0.002)
        b = iso_now()
        # Either equal (low-res clock) or b > a; never b < a.
        assert b >= a


class TestNowMs:
    def test_within_a_second_of_walltime(self) -> None:
        import time

        before_ms = int(time.time() * 1000)
        v = now_ms()
        after_ms = int(time.time() * 1000)
        assert before_ms <= v <= after_ms

    def test_monotonic_in_call_order(self) -> None:
        a = now_ms()
        b = now_ms()
        assert b >= a


class TestNowS:
    def test_within_a_second_of_walltime(self) -> None:
        import time

        before_s = int(time.time())
        v = now_s()
        after_s = int(time.time())
        assert before_s <= v <= after_s

    def test_monotonic_in_call_order(self) -> None:
        a = now_s()
        b = now_s()
        assert b >= a
