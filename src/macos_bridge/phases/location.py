"""CoreLocation-derived sensors via the LocationFetcher Swift helper.

Spawns ``helpers/location-fetcher/.build/release/LocationFetcher.app/
Contents/MacOS/LocationFetcher`` once per tick, parses the JSON it
prints to stdout, and publishes ~10 HA sensors describing where this
Mac currently is — latitude / longitude / altitude / accuracy plus the
reverse-geocoded address (place name, locality, state, country, postal
code).

Why a separate Swift binary? CoreLocation needs an executable that
holds Location Services TCC permission. The bridge daemon process is
the .venv Python interpreter, which would have to be re-granted any
time the venv is rebuilt — an annoying papercut. The Swift helper
sits at a stable path, ad-hoc signed for grant persistence, and is
granted by the user once.

If the helper isn't built or the user hasn't granted Location, the
ticker logs a single throttled WARN per ``_DENIED_WARN_EVERY_SECONDS``
and stays quiet otherwise — no point in noisy log spam when the
condition is steady-state.

Cadence defaults to 300s (5 min). Apple rate-limits CLGeocoder to
~50 reverse geocodes per hour, and Mac location doesn't typically
change every few seconds — a desktop never moves, a laptop moves
when the user moves it. 5 minutes is a reasonable default.
"""

from __future__ import annotations

import json
import logging
import math
import os
import subprocess
import time
from typing import Any

from macos_bridge.mqtt import MqttPublisher, build_discovery_payload
from macos_bridge.phases.base import (
    AbstractTicker,
    screen_time_state_topic,
    screen_time_unique_id,
)

logger = logging.getLogger(__name__)

# Throttle "Location denied" warnings so a permanently-revoked grant
# doesn't flood the log. One log line every 30 minutes is plenty to
# remind the user to fix it without becoming wallpaper.
_DENIED_WARN_EVERY_SECONDS = 1800.0

# HA's MQTT device_tracker integration requires the published state to
# be one of these literals or a configured zone name. Anything else
# (e.g. a free-form locality string) renders awkwardly in HA and breaks
# zone-based automations.
_TRACKER_HOME = "home"
_TRACKER_AWAY = "not_home"


def _haversine_meters(
    lat1: float, lon1: float, lat2: float, lon2: float
) -> float:
    """Great-circle distance between two coordinates, in meters.

    Uses the standard haversine formula with mean Earth radius
    (6 371 000 m). Accuracy is well within ±0.5 % over zone-radius
    distances (a few hundred meters), which is more than enough for
    a home / away decision.
    """
    earth_radius_m = 6_371_000.0
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = (
        math.sin(dphi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    )
    return 2 * earth_radius_m * math.asin(math.sqrt(a))


# (suffix, friendly_name, icon, device_class, unit_of_measurement, state_class)
_SENSORS: list[tuple[str, str, str | None, str | None, str | None, str | None]] = [
    ("latitude", "Location - Latitude", "mdi:latitude", None, "°", None),
    ("longitude", "Location - Longitude", "mdi:longitude", None, "°", None),
    ("altitude", "Location - Altitude", "mdi:altimeter", "distance", "m", "measurement"),
    ("accuracy", "Location - Accuracy", "mdi:crosshairs-gps", "distance", "m", "measurement"),
    ("place_name", "Location - Place Name", "mdi:home-map-marker", None, None, None),
    ("locality", "Location - City", "mdi:city", None, None, None),
    ("administrative_area", "Location - State", "mdi:map", None, None, None),
    ("country", "Location - Country", "mdi:earth", None, None, None),
    ("postal_code", "Location - Postal Code", "mdi:mailbox", None, None, None),
    ("timestamp", "Location - Last Fix At", "mdi:clock-outline", "timestamp", None, None),
]

# JSON-key → sensor-suffix map. Most are 1:1 but a couple are aliases
# so the friendly sensor name doesn't have to leak Apple's terminology.
_FIELD_TO_SUFFIX = {
    "latitude": "latitude",
    "longitude": "longitude",
    "altitude": "altitude",
    "horizontal_accuracy": "accuracy",
    "place_name": "place_name",
    "locality": "locality",
    "administrative_area": "administrative_area",
    "country": "country",
    "postal_code": "postal_code",
    "timestamp": "timestamp",
}


def _spawn_helper(binary_path: str, timeout_seconds: int) -> dict[str, Any]:
    """Run the Swift helper once and return its parsed JSON output.

    The helper always exits 0 and always emits one JSON object — either
    ``{"ok": true, ...}`` on success or ``{"ok": false, "error": "..."}``
    on permission denial / timeout / etc. Any subprocess failure (binary
    missing, killed, garbage stdout) is normalized into the same shape
    so callers have a single uniform parse path.
    """
    if not os.path.isfile(binary_path) or not os.access(binary_path, os.X_OK):
        return {"ok": False, "error": f"binary_missing: {binary_path}"}
    try:
        proc = subprocess.run(
            [binary_path, "--reverse-geocode", "--timeout", str(timeout_seconds)],
            capture_output=True,
            text=True,
            # Add a few seconds of slack on top of the helper's own
            # internal watchdog so we don't kill the helper mid-emit.
            timeout=timeout_seconds + 5,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "subprocess_timeout"}
    except OSError as exc:
        return {"ok": False, "error": f"spawn_failed: {exc}"}

    stdout = proc.stdout.strip()
    if not stdout:
        return {"ok": False, "error": "no_stdout"}
    # The helper emits one JSON object per line; we only need the last.
    last_line = stdout.splitlines()[-1]
    try:
        return json.loads(last_line)
    except json.JSONDecodeError as exc:
        return {"ok": False, "error": f"json_decode: {exc}"}


class LocationTicker(AbstractTicker):
    name = "location"

    def __init__(
        self,
        *,
        enabled: bool,
        interval_seconds: int,
        host_slug: str,
        topic_prefix: str,
        availability_topic: str,
        binary_path: str,
        fetch_timeout_seconds: int = 20,
        home_latitude: float | None = None,
        home_longitude: float | None = None,
        home_radius_meters: float = 100.0,
    ) -> None:
        self._enabled = enabled
        self._interval_seconds = interval_seconds
        self._host_slug = host_slug
        self._topic_prefix = topic_prefix
        self._availability = availability_topic
        self._binary_path = os.path.expanduser(binary_path)
        self._fetch_timeout_seconds = fetch_timeout_seconds
        self._home_latitude = home_latitude
        self._home_longitude = home_longitude
        self._home_radius_meters = home_radius_meters
        self._last_denied_warn_at: float = 0.0

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def interval_seconds(self) -> int | None:
        return self._interval_seconds

    def _state_topic(self, suffix: str) -> str:
        return screen_time_state_topic(
            self._topic_prefix, self._host_slug, f"location/{suffix}"
        )

    def _attrs_topic(self) -> str:
        return screen_time_state_topic(
            self._topic_prefix, self._host_slug, "location/attrs"
        )

    def _device_tracker_state_topic(self) -> str:
        return screen_time_state_topic(
            self._topic_prefix, self._host_slug, "location/device_tracker_state"
        )

    def _device_tracker_attrs_topic(self) -> str:
        # HA's MQTT device_tracker pulls latitude / longitude /
        # gps_accuracy from json_attributes_topic. Use a dedicated topic
        # (rather than reusing location/attrs) so we can emit exactly
        # the keys HA expects without polluting the sensor attrs view
        # with duplicate ``gps_accuracy`` / ``source_type`` fields.
        return screen_time_state_topic(
            self._topic_prefix, self._host_slug, "location/device_tracker_attrs"
        )

    def _unique_id(self, suffix: str) -> str:
        return screen_time_unique_id(self._host_slug, "location", suffix)

    def _device_tracker_unique_id(self) -> str:
        return screen_time_unique_id(self._host_slug, "device_tracker")

    def _maybe_warn_denied(self, reason: str) -> None:
        now = time.monotonic()
        if now - self._last_denied_warn_at < _DENIED_WARN_EVERY_SECONDS:
            return
        self._last_denied_warn_at = now
        logger.warning(
            "location helper failed: %s. Grant Location Services to "
            "%s via System Settings → Privacy & Security → Location "
            "Services (or run the binary once interactively to trigger "
            "the prompt).",
            reason, self._binary_path,
        )

    async def run_once(self, mqtt: MqttPublisher) -> None:
        result = _spawn_helper(self._binary_path, self._fetch_timeout_seconds)
        if not result.get("ok"):
            self._maybe_warn_denied(str(result.get("error", "unknown")))
            return

        # Map JSON fields → sensor suffixes and publish.
        for field, suffix in _FIELD_TO_SUFFIX.items():
            value = result.get(field)
            if value is None or value == "":
                continue
            mqtt.publish_state(self._state_topic(suffix), value)

        # Publish the full payload as attrs so HA templates can read
        # fields without their own entity (sub_locality, country_code,
        # timezone, sub_administrative_area, vertical_accuracy, etc.).
        mqtt.publish_attributes(self._attrs_topic(), result)

        # Drive the HA device_tracker entity. HA's MQTT device_tracker
        # integration requires the published state to be one of "home"
        # / "not_home" / a known zone name — it does NOT auto-resolve
        # coordinates against HA zones the way mobile_app does. So the
        # bridge has to compute home/away itself from a configured
        # home zone. Without home_latitude/longitude, we default to
        # "home" (correct for a desk-bound Mac, wrong for a laptop
        # the user actually carries around — set the home zone in
        # config.yaml in that case).
        latitude = result.get("latitude")
        longitude = result.get("longitude")
        if latitude is not None and longitude is not None:
            home_distance_m: float | None = None
            if (
                self._home_latitude is not None
                and self._home_longitude is not None
            ):
                home_distance_m = _haversine_meters(
                    latitude, longitude,
                    self._home_latitude, self._home_longitude,
                )
                tracker_state = (
                    _TRACKER_HOME
                    if home_distance_m <= self._home_radius_meters
                    else _TRACKER_AWAY
                )
            else:
                # No home zone configured — assume home. Better default
                # than "not_home" since the typical macOS bridge user
                # is running it on a Mac that lives at home.
                tracker_state = _TRACKER_HOME

            mqtt.publish_state(self._device_tracker_state_topic(), tracker_state)
            tracker_attrs = {
                "latitude": latitude,
                "longitude": longitude,
                # HA expects this key name regardless of source. The
                # CoreLocation field is ``horizontal_accuracy``.
                "gps_accuracy": result.get("horizontal_accuracy"),
                "source_type": "gps",
                # Bonus fields surfaced as device-tracker attributes for
                # automations / map popups.
                "place_name": result.get("place_name"),
                "city": result.get("locality"),
                "state": result.get("administrative_area"),
                "country": result.get("country"),
                "country_code": result.get("country_code"),
                "postal_code": result.get("postal_code"),
                "altitude": result.get("altitude"),
                "fix_at": result.get("timestamp"),
                # Surface the computed home distance so the user can
                # see how close to / far from home the Mac is and tune
                # home_radius_meters accordingly.
                "home_distance_m": (
                    round(home_distance_m, 1)
                    if home_distance_m is not None
                    else None
                ),
            }
            # Strip Nones so HA doesn't show blank rows for missing
            # data (e.g. altitude is often unavailable on Wi-Fi fixes,
            # home_distance_m is None when no home zone is configured).
            tracker_attrs = {k: v for k, v in tracker_attrs.items() if v is not None}
            mqtt.publish_attributes(self._device_tracker_attrs_topic(), tracker_attrs)

        logger.info(
            "location tick: %.5f,%.5f ±%.0fm — %s, %s, %s",
            result.get("latitude", 0.0),
            result.get("longitude", 0.0),
            result.get("horizontal_accuracy", 0.0),
            result.get("locality", "?"),
            result.get("administrative_area", "?"),
            result.get("country_code") or result.get("country", "?"),
        )

    async def publish_discovery(self, mqtt: MqttPublisher) -> None:
        device = mqtt.host_device_block()

        for suffix, name, icon, device_class, unit, state_class in _SENSORS:
            uid = self._unique_id(suffix)
            # Attach the rich attrs payload to Latitude (a stable,
            # always-present field) so HA templates can read fields
            # that don't have their own entity.
            attrs_topic = self._attrs_topic() if suffix == "latitude" else None
            mqtt.publish_discovery(
                component="sensor",
                unique_id=uid,
                payload=build_discovery_payload(
                    name=name,
                    unique_id=uid,
                    state_topic=self._state_topic(suffix),
                    # Live data: keep availability_topic so HA flips
                    # to "Unavailable" when the bridge is offline,
                    # signalling stale-vs-current.
                    availability_topic=self._availability,
                    device=device,
                    device_class=device_class,
                    unit_of_measurement=unit,
                    state_class=state_class,
                    icon=icon,
                    json_attributes_topic=attrs_topic,
                ),
            )

        # ---- HA MQTT device_tracker entity ---------------------------
        # Publishes a tracker that HA renders on the Lovelace map and
        # participates in zone detection. The state_topic carries the
        # locality name (e.g. "Springfield"); json_attributes_topic
        # carries latitude/longitude/gps_accuracy in HA's expected
        # field names. With source_type=gps, HA uses the coordinates
        # to compute the actual zone (home/work/away) and overrides
        # the displayed state when a zone matches.
        #
        # See https://www.home-assistant.io/integrations/device_tracker.mqtt/
        tracker_uid = self._device_tracker_unique_id()
        tracker_payload: dict[str, Any] = {
            "name": "Location",
            "unique_id": tracker_uid,
            "object_id": tracker_uid,
            "state_topic": self._device_tracker_state_topic(),
            "json_attributes_topic": self._device_tracker_attrs_topic(),
            "source_type": "gps",
            "icon": "mdi:laptop",
            "device": device,
            # Live tracker — flip to "Unavailable" when the bridge
            # disconnects rather than showing a stale "home" state
            # that's actually unknown.
            "availability_topic": self._availability,
            "payload_available": "online",
            "payload_not_available": "offline",
        }
        mqtt.publish_discovery(
            component="device_tracker",
            unique_id=tracker_uid,
            payload=tracker_payload,
        )
