"""macOS Focus mode (Do Not Disturb / Work / Sleep / Personal / etc.).

Reads the current Focus state from the user's DoNotDisturb data store at
``~/Library/DoNotDisturb/DB/Assertions.json`` plus the human-readable mode
catalog at ``ModeConfigurations.json``. The Assertions JSON is updated in
real time whenever the user activates / deactivates a focus, so a 10s
poll is plenty for HA-side automations that need to respect "do not
disturb" windows.

Publishes:

  sensor.<host>_focus_mode             — active focus name, or "None"
  binary_sensor.<host>_focus_active    — ON when any focus is active
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from macos_bridge.mqtt import MqttPublisher, build_discovery_payload
from macos_bridge.phases.base import (
    AbstractTicker,
    screen_time_state_topic,
    screen_time_unique_id,
)

logger = logging.getLogger(__name__)

_ASSERTIONS_PATH = Path("~/Library/DoNotDisturb/DB/Assertions.json").expanduser()
_MODES_PATH = Path("~/Library/DoNotDisturb/DB/ModeConfigurations.json").expanduser()


def _load_json(path: Path) -> dict | None:
    try:
        with open(path) as f:
            return json.load(f)
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError) as exc:
        logger.debug("focus_mode: could not read %s: %s", path, exc)
        return None


def _mode_name_lookup() -> dict[str, str]:
    """Return ``{mode_uuid: friendly_name}`` from ModeConfigurations.json."""
    cfg = _load_json(_MODES_PATH)
    if not cfg:
        return {}
    out: dict[str, str] = {}
    # The configuration file shape varies by macOS version; defensively
    # walk anything that looks like {modeIdentifier: ..., name: ...}.
    for entry in cfg.get("data", []):
        modes = entry.get("modeConfigurations", {})
        for mode_id, mode in modes.items():
            mc = mode.get("mode") if isinstance(mode, dict) else None
            name = (mc or {}).get("name")
            if name:
                out[mode_id] = name
    return out


def _read_active_focus() -> tuple[str | None, str | None, str | None]:
    """Return (mode_uuid, friendly_name, started_at_iso) for the currently
    active focus, or (None, None, None) when no focus is active.

    Apple stores active assertions inside Assertions.json under
    ``data[].storeAssertionRecords[]``. Each record carries a
    ``modeIdentifier`` and a ``startDateTimestamp`` (Mac Absolute Time).
    """
    blob = _load_json(_ASSERTIONS_PATH)
    if not blob:
        return None, None, None

    name_lookup = _mode_name_lookup()

    best_mode_id: str | None = None
    best_started: float | None = None
    for entry in blob.get("data", []):
        for record in entry.get("storeAssertionRecords", []):
            mode_id = (
                record.get("assertionDetails", {}).get("assertionDetailsModeIdentifier")
                or record.get("modeIdentifier")
            )
            if not mode_id:
                continue
            ts = (
                record.get("assertionStartDateTimestamp")
                or record.get("startDateTimestamp")
            )
            if best_started is None or (ts is not None and ts > best_started):
                best_started = ts
                best_mode_id = mode_id

    if best_mode_id is None:
        return None, None, None

    name = name_lookup.get(best_mode_id) or best_mode_id
    started_iso: str | None = None
    if best_started is not None:
        # Mac Absolute Time → Unix → ISO
        try:
            unix_ts = float(best_started) + 978307200.0
            started_iso = datetime.fromtimestamp(unix_ts, tz=UTC).isoformat()
        except (TypeError, ValueError):
            started_iso = None
    return best_mode_id, name, started_iso


class FocusModeTicker(AbstractTicker):
    name = "focus_mode"

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
        self._published_state: str | None = None

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def interval_seconds(self) -> int | None:
        return self._interval_seconds

    def _state_topic(self, suffix: str) -> str:
        return screen_time_state_topic(
            self._topic_prefix, self._host_slug, f"focus_mode/{suffix}"
        )

    def _unique_id(self, suffix: str) -> str:
        return screen_time_unique_id(self._host_slug, "focus_mode", suffix)

    async def run_once(self, mqtt: MqttPublisher) -> None:
        mode_id, name, started_at = _read_active_focus()
        active = mode_id is not None
        state = name or "None"

        mqtt.publish_state(self._state_topic("active_name"), state)
        mqtt.publish_state(self._state_topic("active"), "ON" if active else "OFF")
        attrs: dict[str, Any] = {
            "active": active,
            "mode_id": mode_id or "",
            "name": state,
            "started_at": started_at or "",
        }
        mqtt.publish_attributes(self._state_topic("active_name") + "/attrs", attrs)

        if state != self._published_state:
            logger.info("focus_mode: %s (mode_id=%s, started_at=%s)",
                        state, mode_id, started_at)
            self._published_state = state

    async def publish_discovery(self, mqtt: MqttPublisher) -> None:
        device = mqtt.host_device_block()

        mqtt.publish_discovery(
            component="sensor",
            unique_id=self._unique_id("active_name"),
            payload=build_discovery_payload(
                name="Focus Mode",
                unique_id=self._unique_id("active_name"),
                state_topic=self._state_topic("active_name"),
                availability_topic=self._availability,
                device=device,
                icon="mdi:moon-waning-crescent",
                json_attributes_topic=self._state_topic("active_name") + "/attrs",
            ),
        )

        bin_payload: dict[str, Any] = build_discovery_payload(
            name="Focus Active",
            unique_id=self._unique_id("active"),
            state_topic=self._state_topic("active"),
            availability_topic=self._availability,
            device=device,
            icon="mdi:bell-off",
        )
        bin_payload["payload_on"] = "ON"
        bin_payload["payload_off"] = "OFF"
        mqtt.publish_discovery(
            component="binary_sensor",
            unique_id=self._unique_id("active"),
            payload=bin_payload,
        )
