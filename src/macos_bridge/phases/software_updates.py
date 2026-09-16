"""Pending macOS software updates.

Runs ``softwareupdate -l --no-scan`` periodically (slow cadence — the
results don't change often, and macOS itself runs the heavier scan in
the background). The ``--no-scan`` flag is critical: a fresh scan can
take 60+ seconds and trip the asyncio timeout, while reading the cache
is essentially instant.

Publishes:

  sensor.<host>_software_updates_count   — number of pending updates
  attrs.updates                          — list of {label, recommended,
                                                    requires_restart}
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

# softwareupdate -l output format (one update per pair of lines):
#   * Label: macOS Sonoma 14.6.1-26G80
#       Title: macOS Sonoma 14.6.1, Version: 14.6.1, Size: 1.32 GiB,
#       Recommended: YES, Action: restart,
_LABEL_RE = re.compile(r"^\s*\*\s+Label:\s+(.+?)\s*$", re.MULTILINE)


def _run_softwareupdate(timeout: float = 30.0) -> str:
    try:
        result = subprocess.run(
            ["softwareupdate", "-l", "--no-scan"],
            capture_output=True, text=True, timeout=timeout, check=False,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
        logger.debug("softwareupdate failed: %s", exc)
        return ""
    return result.stdout or ""


def _parse_updates(text: str) -> list[dict[str, Any]]:
    """Walk the ``softwareupdate -l`` output, returning one dict per update."""
    if not text or "No new software available" in text:
        return []
    updates: list[dict[str, Any]] = []
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        m = _LABEL_RE.match(lines[i])
        if not m:
            i += 1
            continue
        label = m.group(1)
        # Pull the next non-blank line as the metadata blob.
        meta_line = ""
        j = i + 1
        while j < len(lines):
            stripped = lines[j].strip()
            if stripped.startswith("*"):
                break
            if stripped:
                meta_line += " " + stripped
            j += 1
        meta_line = meta_line.lower()
        recommended = "recommended: yes" in meta_line
        requires_restart = "action: restart" in meta_line
        updates.append({
            "label": label,
            "recommended": recommended,
            "requires_restart": requires_restart,
        })
        i = j
    return updates


class SoftwareUpdatesTicker(AbstractTicker):
    name = "software_updates"

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

    def _state_topic(self) -> str:
        return screen_time_state_topic(
            self._topic_prefix, self._host_slug, "software_updates/count"
        )

    def _unique_id(self) -> str:
        return screen_time_unique_id(self._host_slug, "software_updates")

    async def run_once(self, mqtt: MqttPublisher) -> None:
        text = _run_softwareupdate()
        updates = _parse_updates(text)
        count = len(updates)

        mqtt.publish_state(self._state_topic(), count)
        mqtt.publish_attributes(
            self._state_topic() + "/attrs",
            {
                "updates": updates,
                "labels": [u["label"] for u in updates],
                "recommended_count": sum(1 for u in updates if u["recommended"]),
                "requires_restart_count": sum(
                    1 for u in updates if u["requires_restart"]
                ),
            },
        )
        logger.info("software_updates tick: %d pending", count)

    async def publish_discovery(self, mqtt: MqttPublisher) -> None:
        device = mqtt.host_device_block()
        mqtt.publish_discovery(
            component="sensor",
            unique_id=self._unique_id(),
            payload=build_discovery_payload(
                name="Pending Software Updates",
                unique_id=self._unique_id(),
                state_topic=self._state_topic(),
                availability_topic=None,
                device=device,
                state_class="measurement",
                unit_of_measurement="count",
                icon="mdi:apple-finder",
                entity_category="diagnostic",
                json_attributes_topic=self._state_topic() + "/attrs",
            ),
        )
