"""Phase F — system-state sensors.

Polls every `interval_seconds` (default 30s) and publishes:

  sensor.<host>_wifi_ssid                  — current SSID (or "" if not on Wi-Fi)
  sensor.<host>_bluetooth_connected_count  — connected BT devices (names as attr)
  sensor.<host>_screen_brightness          — 0-100 (raw + millinits as attrs)
  sensor.<host>_volume_level               — 0-100 (output volume)
  binary_sensor.<host>_volume_muted        — output mute state
  binary_sensor.<host>_lid_closed          — clamshell state
  sensor.<host>_battery_percent            — 0-100
  binary_sensor.<host>_battery_charging    — AC power / charging state

All detection is permission-free except Wi-Fi SSID, which on macOS 14+
requires Location Services granted to the daemon's python binary.
"""

from __future__ import annotations

import logging
import re
import subprocess
from typing import Any

from macos_bridge.mqtt import MqttPublisher, build_discovery_payload
from macos_bridge.phases.base import (
    AbstractTicker,
    screen_time_state_topic,
    screen_time_unique_id,
)

logger = logging.getLogger(__name__)


_BRIGHTNESS_RE = re.compile(
    r'"brightness"=\{"min"=\d+,"max"=(\d+),"value"=(\d+)\}'
)
_BRIGHTNESS_MILLINITS_RE = re.compile(
    r'"BrightnessMilliNits"=\{"min"=\d+,"value"=(\d+),"uncalMilliNits"=(\d+),"max"=(\d+)\}'
)
_RAW_BRIGHTNESS_RE = re.compile(
    r'"rawBrightness"=\{"min"=\d+,"max"=(\d+),"value"=(\d+)\}'
)
_CLAMSHELL_RE = re.compile(r'"AppleClamshellState"\s*=\s*(\w+)')
_AIRPORT_SSID_RE = re.compile(r"Current Wi-?Fi Network: (.+?)$", re.MULTILINE)
# Anchor on a line beginning with "Connected:" so we don't match "Not Connected:".
_BT_CONNECTED_BLOCK_RE = re.compile(
    r"^\s*Connected:(.*?)(?=^\s*Not Connected:|\Z)", re.MULTILINE | re.DOTALL
)
_BT_DEVICE_NAME_RE = re.compile(r"^\s{6,10}([^\s].*?):\s*$", re.MULTILINE)
# Battery field appears as `Battery Level: 80%` (or `Left/Right/Case` variants
# for AirPods). We capture the device-name parent + the integer percent so we
# can surface a per-device map of last-known battery levels.
_BT_BATTERY_RE = re.compile(
    r"^(\s+)([^\s].*?):\s*$\n((?:\1\s+.*\n)*?)\1\s+Battery Level(?:\s*\(([^)]+)\))?:\s*(\d+)%",
    re.MULTILINE,
)
_BATTERY_RE = re.compile(r"InternalBattery.*?(\d+)%;\s*([\w-]+);", re.DOTALL)
_AC_POWER_RE = re.compile(r"Now drawing from '(.*?)'")


def _run(args: list[str], timeout: float = 3.0) -> str | None:
    try:
        result = subprocess.run(
            args, capture_output=True, text=True, timeout=timeout, check=False
        )
    except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
        logger.debug("%s failed: %s", args[0], exc)
        return None
    return result.stdout if result.returncode == 0 else None


# Sentinel returned in place of the real SSID when Location Services hasn't
# been granted to whichever binary is calling system_profiler — surfaced
# to HA so the user immediately sees what's wrong (rather than the
# inscrutable "<redacted>" macOS fills in).
_SSID_LOCATION_DENIED = "(location services denied)"

# How often to remind the user via the log that the SSID redaction is
# fixable. The probe runs every system_state interval (default 30s) and
# spamming the log every tick would be obnoxious. Once per ~5 min is
# enough to be actionable.
_SSID_LOCATION_WARN_EVERY_SECONDS = 300.0
# None until the first warning (monotonic() counts from boot; see location.py).
_last_location_warn_at: float | None = None


def _wifi_ssid(interface: str) -> str | None:
    """Current Wi-Fi SSID.

    Returns:
      * the SSID string if associated AND Location Services is granted
      * ``""`` when Wi-Fi is off or not associated to any network
      * ``_SSID_LOCATION_DENIED`` ("(location services denied)") when
        macOS redacted the SSID because the calling binary doesn't hold
        the Location grant. Better than surfacing the raw "<redacted>"
        token to HA — the value is now self-explanatory, and the
        ``permissions`` ticker also flags the missing grant.
      * ``None`` on read failure (subprocess timeout / spawn error), so
        the caller can skip the publish and preserve the broker's
        last-known-good retained value.
    """
    out = _run(
        ["system_profiler", "SPAirPortDataType", "-detailLevel", "basic"],
        timeout=15.0,
    )
    if out is None:
        return None
    lines = out.splitlines()
    for i, line in enumerate(lines):
        if "Current Network Information:" not in line:
            continue
        for j in range(i + 1, min(i + 5, len(lines))):
            candidate = lines[j].rstrip()
            if not candidate.endswith(":"):
                continue
            ssid = candidate.strip().rstrip(":").strip()
            if not ssid:
                continue
            if ssid == "<redacted>":
                _maybe_warn_location_denied()
                return _SSID_LOCATION_DENIED
            return ssid
    return ""


def _maybe_warn_location_denied() -> None:
    """Emit one actionable WARNING per ``_SSID_LOCATION_WARN_EVERY_SECONDS``
    so the user sees the fix without log spam."""
    global _last_location_warn_at
    import time as _time
    now = _time.monotonic()
    if (_last_location_warn_at is not None
            and now - _last_location_warn_at < _SSID_LOCATION_WARN_EVERY_SECONDS):
        return
    _last_location_warn_at = now
    logger.warning(
        "Wi-Fi SSID redacted by macOS — Location Services is not granted "
        "to this bridge's python binary. Open System Settings → Privacy "
        "& Security → Location Services and enable the entry for "
        "<bridge-dir>/.venv/bin/python (the `permissions` ticker also "
        "reports this as `location_services: denied`)."
    )


def _bluetooth_connected() -> tuple[int, list[str], dict[str, int]]:
    """Return (count, ordered_names, batteries) where batteries is a dict
    mapping ``device name`` (or ``device name (Left|Right|Case)``) to the
    integer percent. Devices that don't broadcast a battery level are
    omitted from the dict.
    """
    out = _run(["system_profiler", "SPBluetoothDataType", "-detailLevel", "basic"], timeout=10.0)
    if out is None:
        return 0, [], {}
    block = _BT_CONNECTED_BLOCK_RE.search(out)
    if not block:
        return 0, [], {}
    block_text = block.group(1)
    names = [n.strip() for n in _BT_DEVICE_NAME_RE.findall(block_text)]
    batteries: dict[str, int] = {}
    for m in _BT_BATTERY_RE.finditer(block_text):
        device = m.group(2).strip()
        slot = (m.group(4) or "").strip()
        try:
            pct = int(m.group(5))
        except ValueError:
            continue
        key = f"{device} ({slot})" if slot else device
        batteries[key] = pct
    return len(names), names, batteries


def _screen_brightness() -> dict[str, Any]:
    """Return brightness fields. percent is 0-100; raw 0-2047; millinits is luminance.

    Apple Silicon XDR displays scale 0–`max`, where `max/2` ≈ slider's 100%
    in System Settings. We compute percent as `value / (max/2) * 100` clipped
    to 100, falling back to plain `value/max` for older / external displays.
    """
    out = _run(["ioreg", "-r", "-d", "1", "-k", "IODisplayParameters"])
    if out is None:
        return {"percent": None, "raw": None, "millinits": None}
    bm = _BRIGHTNESS_RE.search(out)
    rb = _RAW_BRIGHTNESS_RE.search(out)
    mn = _BRIGHTNESS_MILLINITS_RE.search(out)
    percent: float | None = None
    if bm:
        max_val = int(bm.group(1))
        cur = int(bm.group(2))
        if max_val:
            sdr_max = max_val / 2 if max_val >= 1024 else max_val
            percent = round(min(cur / sdr_max, 1.0) * 100.0, 1)
    return {
        "percent": percent,
        "raw": int(rb.group(2)) if rb else None,
        "millinits": int(mn.group(1)) if mn else None,
    }


def _volume() -> tuple[int | None, bool | None]:
    level_out = _run(["osascript", "-e", "output volume of (get volume settings)"])
    muted_out = _run(["osascript", "-e", "output muted of (get volume settings)"])
    level: int | None = None
    if level_out is not None:
        try:
            level = int(level_out.strip())
        except ValueError:
            pass
    muted: bool | None = None
    if muted_out is not None:
        muted = muted_out.strip().lower() == "true"
    return level, muted


def _lid_closed() -> bool | None:
    out = _run(["ioreg", "-r", "-k", "AppleClamshellState"])
    if out is None:
        return None
    match = _CLAMSHELL_RE.search(out)
    if not match:
        return None
    return match.group(1).strip().lower() == "yes"


def _battery() -> tuple[int | None, bool | None]:
    out = _run(["pmset", "-g", "batt"])
    if out is None:
        return None, None
    percent: int | None = None
    charging: bool | None = None
    bm = _BATTERY_RE.search(out)
    if bm:
        try:
            percent = int(bm.group(1))
        except ValueError:
            percent = None
        state = bm.group(2).lower()
        charging = state in ("charging", "finishing-charge", "ac")
    ac = _AC_POWER_RE.search(out)
    if ac and "AC Power" in ac.group(1):
        charging = True
    return percent, charging


def _bool_to_payload(value: bool) -> str:
    return "ON" if value else "OFF"


def _screensaver_running() -> bool:
    """``True`` if ScreenSaverEngine is the live process driving the
    screen. macOS spawns this single binary whether the screensaver
    was started by the system idle timer, the hot corner, or our own
    ``open -a ScreenSaverEngine`` command, so a process check is the
    canonical signal. Used to drive both a binary_sensor and the
    Controls - Screensaver switch's read side."""
    try:
        result = subprocess.run(
            ["pgrep", "-x", "ScreenSaverEngine"],
            capture_output=True, timeout=2.0, check=False,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return False
    return result.returncode == 0


class SystemStateTicker(AbstractTicker):
    name = "system_state"

    def __init__(
        self,
        *,
        enabled: bool,
        interval_seconds: int,
        host_slug: str,
        topic_prefix: str,
        availability_topic: str,
        wifi_interface: str = "en0",
    ) -> None:
        self._enabled = enabled
        self._interval_seconds = interval_seconds
        self._host_slug = host_slug
        self._topic_prefix = topic_prefix
        self._availability = availability_topic
        self._wifi_interface = wifi_interface

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def interval_seconds(self) -> int | None:
        return self._interval_seconds

    def _state_topic(self, suffix: str) -> str:
        return screen_time_state_topic(
            self._topic_prefix, self._host_slug, f"system/{suffix}"
        )

    def _unique_id(self, suffix: str) -> str:
        return screen_time_unique_id(self._host_slug, "system", suffix)

    async def run_once(self, mqtt: MqttPublisher) -> None:
        ssid = _wifi_ssid(self._wifi_interface)
        bt_count, bt_names, bt_batteries = _bluetooth_connected()
        brightness = _screen_brightness()
        vol_level, vol_muted = _volume()
        lid_closed = _lid_closed()
        bat_pct, bat_charging = _battery()
        screensaver_on = _screensaver_running()

        if ssid is not None:
            mqtt.publish_state(self._state_topic("wifi_ssid"), ssid)
        mqtt.publish_state(self._state_topic("bluetooth_connected_count"), bt_count)
        mqtt.publish_attributes(
            self._state_topic("bluetooth_connected_count") + "/attrs",
            {
                "connected_devices": bt_names,
                # Per-device battery percent, e.g.
                #   {"AirPods Pro (Left)": 80, "AirPods Pro (Right)": 80,
                #    "AirPods Pro (Case)": 95, "Magic Mouse": 42}
                "device_batteries": bt_batteries,
            },
        )
        if brightness["percent"] is not None:
            mqtt.publish_state(self._state_topic("screen_brightness"), brightness["percent"])
            mqtt.publish_attributes(
                self._state_topic("screen_brightness") + "/attrs",
                {
                    "raw": brightness["raw"],
                    "millinits": brightness["millinits"],
                },
            )
        if vol_level is not None:
            mqtt.publish_state(self._state_topic("volume_level"), vol_level)
        if vol_muted is not None:
            mqtt.publish_state(self._state_topic("volume_muted"), _bool_to_payload(vol_muted))
        if lid_closed is not None:
            mqtt.publish_state(self._state_topic("lid_closed"), _bool_to_payload(lid_closed))
        if bat_pct is not None:
            mqtt.publish_state(self._state_topic("battery_percent"), bat_pct)
        if bat_charging is not None:
            mqtt.publish_state(self._state_topic("battery_charging"), _bool_to_payload(bat_charging))
        # Always publish — controls.py's Screensaver switch reads from
        # this topic, and HA needs a current state for the toggle to
        # render correctly even when the screensaver isn't active.
        mqtt.publish_state(
            self._state_topic("screensaver_running"), _bool_to_payload(screensaver_on)
        )

        logger.info(
            "system_state tick: ssid=%r bt=%d brightness=%s vol=%s muted=%s lid=%s bat=%s charging=%s saver=%s",
            ssid, bt_count, brightness["percent"], vol_level, vol_muted,
            lid_closed, bat_pct, bat_charging, screensaver_on,
        )

    async def publish_discovery(self, mqtt: MqttPublisher) -> None:
        device = mqtt.host_device_block()

        sensors = [
            ("wifi_ssid", "Wi-Fi SSID", None, None, "mdi:wifi", None, None),
            ("bluetooth_connected_count", "Bluetooth Connected", None, None,
             "mdi:bluetooth-connect", "measurement", "attrs"),
            ("screen_brightness", "Screen Brightness", None, "%",
             "mdi:brightness-7", "measurement", "attrs"),
            ("volume_level", "Volume Level", None, "%", "mdi:volume-high", "measurement", None),
            ("battery_percent", "Battery", "battery", "%", "mdi:battery", "measurement", None),
        ]
        for suffix, name, device_class, unit, icon, state_class, attrs_suffix in sensors:
            mqtt.publish_discovery(
                component="sensor",
                unique_id=self._unique_id(suffix),
                payload=build_discovery_payload(
                    name=name,
                    unique_id=self._unique_id(suffix),
                    state_topic=self._state_topic(suffix),
                    availability_topic=self._availability,
                    device=device,
                    device_class=device_class,
                    unit_of_measurement=unit,
                    state_class=state_class,
                    icon=icon,
                    json_attributes_topic=(
                        self._state_topic(suffix) + "/attrs" if attrs_suffix else None
                    ),
                ),
            )

        for suffix, name, icon, device_class in [
            ("volume_muted", "Volume Muted", "mdi:volume-off", None),
            ("lid_closed", "Lid Closed", "mdi:laptop-off", None),
            ("battery_charging", "Battery Charging", "mdi:power-plug", "battery_charging"),
            ("screensaver_running", "Screensaver Running", "mdi:monitor-screenshot", None),
        ]:
            payload: dict[str, Any] = build_discovery_payload(
                name=name,
                unique_id=self._unique_id(suffix),
                state_topic=self._state_topic(suffix),
                availability_topic=self._availability,
                device=device,
                device_class=device_class,
                icon=icon,
            )
            payload["payload_on"] = "ON"
            payload["payload_off"] = "OFF"
            mqtt.publish_discovery(
                component="binary_sensor",
                unique_id=self._unique_id(suffix),
                payload=payload,
            )
