"""Tests for the location phase ticker.

The Swift LocationFetcher helper is stubbed at the module level so the
tests don't actually try to spawn a binary or hit CoreLocation on the
host running pytest.
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest

from macos_bridge.phases import location
from macos_bridge.phases.location import LocationTicker


@pytest.fixture
def fake_mqtt() -> MagicMock:
    m = MagicMock()
    m.host_slug = "test_host"
    m.sw_version = "0.1.0"
    return m


def _ticker(
    *,
    home_latitude: float | None = None,
    home_longitude: float | None = None,
    home_radius_meters: float = 100.0,
) -> LocationTicker:
    return LocationTicker(
        enabled=True,
        interval_seconds=300,
        host_slug="test_host",
        topic_prefix="macos",
        availability_topic="macos/test_host/status",
        binary_path="/nonexistent/LocationFetcher",
        fetch_timeout_seconds=5,
        home_latitude=home_latitude,
        home_longitude=home_longitude,
        home_radius_meters=home_radius_meters,
    )


def _state_calls(fake_mqtt: MagicMock) -> dict[str, object]:
    return {c.args[0]: c.args[1] for c in fake_mqtt.publish_state.call_args_list}


def _attr_calls(fake_mqtt: MagicMock) -> dict[str, dict]:
    return {c.args[0]: c.args[1] for c in fake_mqtt.publish_attributes.call_args_list}


def test_ok_payload_publishes_one_state_per_field(
    monkeypatch: pytest.MonkeyPatch, fake_mqtt: MagicMock
):
    """A successful helper response fans out into one publish_state per
    mapped JSON key, plus one publish_attributes carrying the full
    payload."""

    def fake_spawn(*_args, **_kwargs):
        return {
            "ok": True,
            "latitude": 40.74844,
            "longitude": -73.98566,
            "altitude": 250.5,
            "vertical_accuracy": 4.0,
            "horizontal_accuracy": 35.0,
            "timestamp": "2026-01-01T12:00:00Z",
            "place_name": "350 Fifth Avenue",
            "locality": "New York",
            "sub_locality": "Midtown",
            "administrative_area": "NY",
            "country": "United States",
            "country_code": "US",
            "postal_code": "10118",
            "timezone": "America/New_York",
        }

    monkeypatch.setattr(location, "_spawn_helper", fake_spawn)
    asyncio.run(_ticker().run_once(fake_mqtt))

    states = _state_calls(fake_mqtt)
    # All ten advertised sensors should have a published state.
    expected_topics = {
        "macos/test_host/location/latitude",
        "macos/test_host/location/longitude",
        "macos/test_host/location/altitude",
        "macos/test_host/location/accuracy",
        "macos/test_host/location/place_name",
        "macos/test_host/location/locality",
        "macos/test_host/location/administrative_area",
        "macos/test_host/location/country",
        "macos/test_host/location/postal_code",
        "macos/test_host/location/timestamp",
    }
    assert expected_topics.issubset(set(states.keys()))
    assert states["macos/test_host/location/locality"] == "New York"
    assert states["macos/test_host/location/postal_code"] == "10118"

    attrs = _attr_calls(fake_mqtt)["macos/test_host/location/attrs"]
    # The attrs payload must carry every key from the helper response so
    # HA templates can read fields that don't have their own entity.
    assert attrs["sub_locality"] == "Midtown"
    assert attrs["country_code"] == "US"
    assert attrs["timezone"] == "America/New_York"


def test_failure_payload_publishes_nothing_and_warns_once(
    monkeypatch: pytest.MonkeyPatch, fake_mqtt: MagicMock, caplog: pytest.LogCaptureFixture
):
    """When the helper reports authorization_denied (or any other
    failure) we must not publish stale data — the previous retained
    value in HA is more accurate than zeros — and we should log one
    actionable warning the user can act on."""
    monkeypatch.setattr(
        location, "_spawn_helper", lambda *a, **kw: {"ok": False, "error": "authorization_denied"}
    )
    caplog.set_level("WARNING", logger="macos_bridge.phases.location")
    ticker = _ticker()
    asyncio.run(ticker.run_once(fake_mqtt))

    fake_mqtt.publish_state.assert_not_called()
    fake_mqtt.publish_attributes.assert_not_called()
    assert any(
        "authorization_denied" in r.getMessage() for r in caplog.records
    )


def test_denied_warning_is_throttled(
    monkeypatch: pytest.MonkeyPatch, fake_mqtt: MagicMock, caplog: pytest.LogCaptureFixture
):
    """A permanently denied grant could log every interval — the
    ticker should emit at most one warning per
    ``_DENIED_WARN_EVERY_SECONDS`` window."""
    monkeypatch.setattr(
        location, "_spawn_helper", lambda *a, **kw: {"ok": False, "error": "authorization_denied"}
    )
    caplog.set_level("WARNING", logger="macos_bridge.phases.location")

    ticker = _ticker()
    asyncio.run(ticker.run_once(fake_mqtt))
    asyncio.run(ticker.run_once(fake_mqtt))
    asyncio.run(ticker.run_once(fake_mqtt))

    warn_lines = [r for r in caplog.records if "authorization_denied" in r.getMessage()]
    assert len(warn_lines) == 1


def test_publishes_ten_sensors_plus_device_tracker_discovery(fake_mqtt: MagicMock):
    """One sensor per advertised location field, all carrying
    availability_topic (location is live data, not historical), plus
    one device_tracker entity that drives the HA Lovelace map."""
    asyncio.run(_ticker().publish_discovery(fake_mqtt))
    # 10 sensors + 1 device_tracker = 11 discovery publishes.
    assert fake_mqtt.publish_discovery.call_count == 11

    by_uid = {
        c.kwargs["unique_id"]: c.kwargs["payload"]
        for c in fake_mqtt.publish_discovery.call_args_list
    }
    assert "macos_test_host_location_latitude" in by_uid
    assert "macos_test_host_location_postal_code" in by_uid

    lat = by_uid["macos_test_host_location_latitude"]
    assert lat["name"] == "Location - Latitude"
    assert lat["unit_of_measurement"] == "°"
    # Latitude carries the rich attrs payload for HA templates.
    assert lat["json_attributes_topic"] == "macos/test_host/location/attrs"
    # Live data, not historical — keep the LWT availability topic.
    assert lat["availability_topic"] == "macos/test_host/status"

    last_fix = by_uid["macos_test_host_location_timestamp"]
    assert last_fix["device_class"] == "timestamp"

    altitude = by_uid["macos_test_host_location_altitude"]
    assert altitude["device_class"] == "distance"
    assert altitude["unit_of_measurement"] == "m"

    tracker = by_uid["macos_test_host_device_tracker"]
    assert tracker["name"] == "Location"
    # Source type "gps" tells HA to use json_attributes lat/lon as the
    # authoritative position and compute zones from coordinates.
    assert tracker["source_type"] == "gps"
    assert tracker["state_topic"] == "macos/test_host/location/device_tracker_state"
    assert tracker["json_attributes_topic"] == (
        "macos/test_host/location/device_tracker_attrs"
    )
    assert tracker["availability_topic"] == "macos/test_host/status"


def _ok_payload(*, lat: float = 40.74844, lon: float = -73.98566) -> dict:
    return {
        "ok": True,
        "latitude": lat,
        "longitude": lon,
        "horizontal_accuracy": 35.0,
        "timestamp": "2026-01-01T12:00:00Z",
        "place_name": "350 Fifth Avenue",
        "locality": "New York",
        "administrative_area": "NY",
        "country": "United States",
        "country_code": "US",
        "postal_code": "10118",
    }


def test_device_tracker_state_defaults_to_home_when_no_zone_configured(
    monkeypatch: pytest.MonkeyPatch, fake_mqtt: MagicMock
):
    """When home_latitude/home_longitude aren't set, the bridge can't
    compute home/away — fall back to "home" (the typical case for a
    desk-bound Mac that lives at one place). HA-side automations
    that need real away detection should set the home zone in
    config.yaml."""
    monkeypatch.setattr(location, "_spawn_helper", lambda *a, **kw: _ok_payload())
    asyncio.run(_ticker().run_once(fake_mqtt))

    states = _state_calls(fake_mqtt)
    assert states["macos/test_host/location/device_tracker_state"] == "home"

    attrs = _attr_calls(fake_mqtt)["macos/test_host/location/device_tracker_attrs"]
    assert attrs["latitude"] == pytest.approx(40.74844)
    assert attrs["longitude"] == pytest.approx(-73.98566)
    # gps_accuracy is the HA-expected key name; the source field is
    # ``horizontal_accuracy``.
    assert attrs["gps_accuracy"] == pytest.approx(35.0)
    assert attrs["source_type"] == "gps"
    # Bonus surfaced fields for map popups / automations.
    assert attrs["city"] == "New York"
    assert attrs["state"] == "NY"
    assert attrs["postal_code"] == "10118"
    # No home zone configured -> no home_distance_m attr.
    assert "home_distance_m" not in attrs


def test_device_tracker_state_is_home_when_inside_radius(
    monkeypatch: pytest.MonkeyPatch, fake_mqtt: MagicMock
):
    """A fix within ``home_radius_meters`` of the configured home zone
    publishes ``home`` and exposes the computed distance in attrs."""
    home_lat, home_lon = 40.74844, -73.98566
    # ~10 m off the home pin — well inside a 100 m radius.
    monkeypatch.setattr(
        location, "_spawn_helper",
        lambda *a, **kw: _ok_payload(lat=40.74853, lon=-73.98566),
    )
    ticker = _ticker(
        home_latitude=home_lat, home_longitude=home_lon, home_radius_meters=100.0
    )
    asyncio.run(ticker.run_once(fake_mqtt))

    states = _state_calls(fake_mqtt)
    assert states["macos/test_host/location/device_tracker_state"] == "home"

    attrs = _attr_calls(fake_mqtt)["macos/test_host/location/device_tracker_attrs"]
    assert attrs["home_distance_m"] < 100.0


def test_device_tracker_state_is_not_home_outside_radius(
    monkeypatch: pytest.MonkeyPatch, fake_mqtt: MagicMock
):
    """A fix outside ``home_radius_meters`` publishes ``not_home`` so HA
    automations can fire on the away transition."""
    home_lat, home_lon = 40.74844, -73.98566
    # ~5 km away — far outside any reasonable home radius.
    monkeypatch.setattr(
        location, "_spawn_helper",
        lambda *a, **kw: _ok_payload(lat=40.78000, lon=-74.03000),
    )
    ticker = _ticker(
        home_latitude=home_lat, home_longitude=home_lon, home_radius_meters=100.0
    )
    asyncio.run(ticker.run_once(fake_mqtt))

    states = _state_calls(fake_mqtt)
    assert states["macos/test_host/location/device_tracker_state"] == "not_home"

    attrs = _attr_calls(fake_mqtt)["macos/test_host/location/device_tracker_attrs"]
    assert attrs["home_distance_m"] > 1000.0  # well over 1 km


def test_haversine_meters_basic_sanity():
    """Two coordinates at the same point are zero distance apart, and
    ~1 degree of latitude is ~111 km."""
    from macos_bridge.phases.location import _haversine_meters

    assert _haversine_meters(10.0, 20.0, 10.0, 20.0) == pytest.approx(0.0)
    one_deg_lat = _haversine_meters(10.0, 20.0, 11.0, 20.0)
    assert 110_000 < one_deg_lat < 112_000
