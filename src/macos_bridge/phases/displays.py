"""External display detection.

Parses ``system_profiler SPDisplaysDataType`` to enumerate connected
displays. Each display has a name, resolution, and an "Internal" vs
external flag (the built-in laptop display has Connection Type =
Internal). The resulting external count drives "am I docked at the
desk" automations.

Publishes:

  sensor.<host>_external_displays_count   — number of external displays
  binary_sensor.<host>_docked              — ON when ≥ 1 external display
  attrs.displays                           — list of {name, resolution,
                                                       internal}
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


def _run_displays_profile(timeout: float = 10.0) -> str:
    """system_profiler SPDisplaysDataType is moderately slow (~1-3s warm,
    longer cold) but not network-bound. Default timeout 10s."""
    try:
        result = subprocess.run(
            ["system_profiler", "SPDisplaysDataType", "-detailLevel", "mini"],
            capture_output=True, text=True, timeout=timeout, check=False,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
        logger.debug("system_profiler displays failed: %s", exc)
        return ""
    return result.stdout if result.returncode == 0 else ""


def _parse_displays(text: str) -> list[dict[str, Any]]:
    """Extract one dict per Display from the ``Displays:`` section.

    Format (mini):
        Displays:
          Color LCD:
            Display Type: Built-in Liquid Retina XDR Display
            Resolution: 3456 x 2234 Retina
            Connection Type: Internal
          U28E590:
            Resolution: 6016 x 3384
            UI Looks like: 3008 x 1692 @ 60.00Hz
    """
    if not text:
        return []
    lines = text.splitlines()
    in_displays = False
    base_indent: int | None = None
    displays: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None

    for raw in lines:
        stripped = raw.rstrip()
        if not stripped:
            continue
        indent = len(stripped) - len(stripped.lstrip(" "))
        content = stripped.strip()

        if content.startswith("Displays:") and content.endswith(":"):
            in_displays = True
            base_indent = None
            continue
        if not in_displays:
            continue

        if base_indent is None:
            base_indent = indent

        # New display heading: indent == base_indent and ends with ':' but
        # has no embedded ':' before the trailing one (i.e. not a key:value).
        if (
            indent == base_indent
            and content.endswith(":")
            and content.count(":") == 1
        ):
            if current is not None:
                displays.append(current)
            current = {
                "name": content.rstrip(":"),
                "resolution": None,
                "internal": False,
            }
            continue

        if current is not None and ":" in content:
            key, _, value = content.partition(":")
            key = key.strip().lower()
            value = value.strip()
            if key == "resolution":
                current["resolution"] = value
            elif key == "connection type":
                current["internal"] = value.lower() == "internal"
            elif key == "display type" and "built-in" in value.lower():
                # Older formats omit Connection Type for the laptop panel.
                current["internal"] = True

    if current is not None:
        displays.append(current)
    return displays


class DisplaysTicker(AbstractTicker):
    name = "displays"

    def __init__(
        self,
        *,
        enabled: bool,
        interval_seconds: int,
        host_slug: str,
        topic_prefix: str,
        availability_topic: str,
    ) -> None:
        self._enabled = enabled
        self._interval_seconds = interval_seconds
        self._host_slug = host_slug
        self._topic_prefix = topic_prefix
        self._availability = availability_topic

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def interval_seconds(self) -> int | None:
        return self._interval_seconds

    def _state_topic(self, suffix: str) -> str:
        return screen_time_state_topic(
            self._topic_prefix, self._host_slug, f"displays/{suffix}"
        )

    def _unique_id(self, suffix: str) -> str:
        return screen_time_unique_id(self._host_slug, "displays", suffix)

    async def run_once(self, mqtt: MqttPublisher) -> None:
        displays = _parse_displays(_run_displays_profile())
        external = [d for d in displays if not d.get("internal")]
        external_count = len(external)
        docked = external_count > 0

        mqtt.publish_state(self._state_topic("external_count"), external_count)
        mqtt.publish_state(self._state_topic("docked"), "ON" if docked else "OFF")
        mqtt.publish_attributes(
            self._state_topic("external_count") + "/attrs",
            {
                "displays": displays,
                "external_displays": external,
                "internal_displays": [
                    d for d in displays if d.get("internal")
                ],
                "external_names": [d["name"] for d in external],
            },
        )
        logger.info(
            "displays tick: total=%d external=%d docked=%s",
            len(displays), external_count, docked,
        )

    async def publish_discovery(self, mqtt: MqttPublisher) -> None:
        device = mqtt.host_device_block()
        mqtt.publish_discovery(
            component="sensor",
            unique_id=self._unique_id("external_count"),
            payload=build_discovery_payload(
                name="External Displays",
                unique_id=self._unique_id("external_count"),
                state_topic=self._state_topic("external_count"),
                availability_topic=self._availability,
                device=device,
                state_class="measurement",
                unit_of_measurement="count",
                icon="mdi:monitor-multiple",
                json_attributes_topic=self._state_topic("external_count") + "/attrs",
            ),
        )
        bin_payload: dict[str, Any] = build_discovery_payload(
            name="Docked",
            unique_id=self._unique_id("docked"),
            state_topic=self._state_topic("docked"),
            availability_topic=self._availability,
            device=device,
            icon="mdi:laptop-account",
        )
        bin_payload["payload_on"] = "ON"
        bin_payload["payload_off"] = "OFF"
        mqtt.publish_discovery(
            component="binary_sensor",
            unique_id=self._unique_id("docked"),
            payload=bin_payload,
        )
