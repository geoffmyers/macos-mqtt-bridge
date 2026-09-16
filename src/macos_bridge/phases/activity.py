"""Phase E — device-activity binary sensors.

Polls every `interval_seconds` (default 5s) and publishes:

  binary_sensor.<host>_camera_in_use      — VDCAssistant kCameraStream events
  binary_sensor.<host>_microphone_in_use  — coremedia AudioCapture events
  binary_sensor.<host>_audio_playing      — coreaudiod assertion in pmset
  binary_sensor.<host>_input_active       — HIDIdleTime < threshold
  sensor.<host>_input_idle_seconds        — HIDIdleTime in seconds (numeric)

No TCC permissions required.
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

_CAMERA_PREDICATE = (
    'subsystem == "com.apple.cmio.VDCAssistant" '
    'OR composedMessage CONTAINS "kCameraStream"'
)
_MIC_PREDICATE = (
    'subsystem == "com.apple.coremedia" '
    'AND composedMessage CONTAINS "AudioCapture"'
)

_HID_IDLE_RE = re.compile(r'"HIDIdleTime"\s*=\s*(\d+)')


def _ioreg_idle_seconds() -> float | None:
    try:
        result = subprocess.run(
            ["ioreg", "-c", "IOHIDSystem"],
            capture_output=True, text=True, timeout=2.0, check=False,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
        logger.warning("ioreg failed: %s", exc)
        return None
    if result.returncode != 0:
        return None
    match = _HID_IDLE_RE.search(result.stdout)
    if not match:
        return None
    return int(match.group(1)) / 1e9


def _log_show_recent(predicate: str, window_seconds: int) -> str:
    try:
        result = subprocess.run(
            [
                "log", "show",
                "--last", f"{window_seconds}s",
                "--predicate", predicate,
                "--style", "compact",
            ],
            capture_output=True, text=True, timeout=5.0, check=False,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
        logger.warning("log show failed: %s", exc)
        return ""
    return result.stdout if result.returncode == 0 else ""


def _camera_in_use(window_seconds: int) -> bool:
    text = _log_show_recent(_CAMERA_PREDICATE, window_seconds)
    last_state: bool | None = None
    for line in text.splitlines():
        if "kCameraStreamStart" in line:
            last_state = True
        elif "kCameraStreamStop" in line:
            last_state = False
    return bool(last_state)


def _microphone_in_use(window_seconds: int) -> bool:
    text = _log_show_recent(_MIC_PREDICATE, window_seconds)
    last_state: bool | None = None
    for raw in text.splitlines():
        line = raw.lower()
        if "audiocapture" in line and ("start" in line or "begin" in line):
            last_state = True
        elif "audiocapture" in line and ("stop" in line or "end" in line or "release" in line):
            last_state = False
    return bool(last_state)


def _audio_playing() -> bool:
    try:
        result = subprocess.run(
            ["pmset", "-g", "assertions"],
            capture_output=True, text=True, timeout=2.0, check=False,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return False
    if result.returncode != 0:
        return False
    return "coreaudiod" in result.stdout.lower()


def _bool_to_payload(value: bool) -> str:
    return "ON" if value else "OFF"


class ActivityTicker(AbstractTicker):
    name = "activity"

    def __init__(
        self,
        *,
        enabled: bool,
        interval_seconds: int,
        host_slug: str,
        topic_prefix: str,
        availability_topic: str,
        input_active_threshold_seconds: int = 5,
        camera_log_window_seconds: int = 10,
        microphone_log_window_seconds: int = 10,
    ) -> None:
        self._enabled = enabled
        self._interval_seconds = interval_seconds
        self._host_slug = host_slug
        self._topic_prefix = topic_prefix
        self._availability = availability_topic
        self._input_threshold = input_active_threshold_seconds
        self._camera_window = camera_log_window_seconds
        self._microphone_window = microphone_log_window_seconds

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def interval_seconds(self) -> int | None:
        return self._interval_seconds

    def _state_topic(self, suffix: str) -> str:
        return screen_time_state_topic(
            self._topic_prefix, self._host_slug, f"activity/{suffix}"
        )

    def _unique_id(self, suffix: str) -> str:
        return screen_time_unique_id(self._host_slug, "activity", suffix)

    async def run_once(self, mqtt: MqttPublisher) -> None:
        idle = _ioreg_idle_seconds()
        idle_int = int(idle) if idle is not None else -1
        input_active = idle is not None and idle < self._input_threshold

        camera = _camera_in_use(self._camera_window)
        mic = _microphone_in_use(self._microphone_window)
        audio = _audio_playing()

        mqtt.publish_state(self._state_topic("input_idle_seconds"), idle_int)
        mqtt.publish_state(self._state_topic("input_active"), _bool_to_payload(input_active))
        mqtt.publish_state(self._state_topic("camera_in_use"), _bool_to_payload(camera))
        mqtt.publish_state(self._state_topic("microphone_in_use"), _bool_to_payload(mic))
        mqtt.publish_state(self._state_topic("audio_playing"), _bool_to_payload(audio))

        logger.debug(
            "activity: idle=%ds input_active=%s camera=%s mic=%s audio=%s",
            idle_int, input_active, camera, mic, audio,
        )

    async def publish_discovery(self, mqtt: MqttPublisher) -> None:
        device = mqtt.host_device_block()

        mqtt.publish_discovery(
            component="sensor",
            unique_id=self._unique_id("input_idle_seconds"),
            payload=build_discovery_payload(
                name="Input Idle",
                unique_id=self._unique_id("input_idle_seconds"),
                state_topic=self._state_topic("input_idle_seconds"),
                availability_topic=self._availability,
                device=device,
                device_class="duration",
                unit_of_measurement="s",
                state_class="measurement",
                icon="mdi:timer-sand",
            ),
        )

        for suffix, name, icon in [
            ("input_active", "Input Active", "mdi:keyboard"),
            ("camera_in_use", "Camera In Use", "mdi:camera"),
            ("microphone_in_use", "Microphone In Use", "mdi:microphone"),
            ("audio_playing", "Audio Playing", "mdi:speaker"),
        ]:
            payload: dict[str, Any] = build_discovery_payload(
                name=name,
                unique_id=self._unique_id(suffix),
                state_topic=self._state_topic(suffix),
                availability_topic=self._availability,
                device=device,
                icon=icon,
            )
            payload["payload_on"] = "ON"
            payload["payload_off"] = "OFF"
            mqtt.publish_discovery(
                component="binary_sensor",
                unique_id=self._unique_id(suffix),
                payload=payload,
            )
