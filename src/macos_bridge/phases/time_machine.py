"""Time Machine backup status.

Reads the system's current backup state via ``tmutil status`` and the
last successful backup timestamp via ``tmutil latestbackup``. Both
commands are cheap (no network, no scanning), so this can run on a
medium cadence without daemon impact.

Publishes:

  binary_sensor.<host>_time_machine_running   — ON during an active backup
  sensor.<host>_time_machine_progress         — 0-100 percent (when running)
  sensor.<host>_time_machine_last_backup      — ISO timestamp
"""

from __future__ import annotations

import logging
import re
import subprocess
from datetime import UTC, datetime
from typing import Any

from macos_bridge.mqtt import MqttPublisher, build_discovery_payload
from macos_bridge.phases.base import (
    AbstractTicker,
    screen_time_state_topic,
    screen_time_unique_id,
)

logger = logging.getLogger(__name__)

# tmutil status output is a NeXT-style plist printed inline. Extract the
# fields we care about with regex rather than a real parser — robust to
# Apple adding new keys.
_RUNNING_RE = re.compile(r'Running\s*=\s*([01])')
_PERCENT_RE = re.compile(r'Percent\s*=\s*"?([\-0-9.]+)"?')
_PHASE_RE = re.compile(r'BackupPhase\s*=\s*"?([\w\.]+)"?')


def _run_tmutil(args: list[str], timeout: float = 5.0) -> str:
    try:
        result = subprocess.run(
            ["tmutil", *args],
            capture_output=True, text=True, timeout=timeout, check=False,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
        logger.debug("tmutil %s failed: %s", " ".join(args), exc)
        return ""
    if result.returncode != 0:
        return ""
    return result.stdout or ""


def _parse_status(text: str) -> tuple[bool, float | None, str | None]:
    """Return (running, percent_0_to_100_or_None, phase_or_None)."""
    running_m = _RUNNING_RE.search(text)
    running = running_m is not None and running_m.group(1) == "1"

    percent: float | None = None
    pm = _PERCENT_RE.search(text)
    if pm:
        try:
            raw = float(pm.group(1))
            # tmutil reports -1.0 when not backing up; otherwise 0.0–1.0.
            if raw >= 0:
                percent = round(raw * 100.0, 1)
        except ValueError:
            percent = None

    phase: str | None = None
    phm = _PHASE_RE.search(text)
    if phm:
        phase = phm.group(1)
    return running, percent, phase


def _last_backup_iso() -> str | None:
    """``tmutil latestbackup`` prints the path of the most recent local
    backup; the timestamp is encoded in the directory name as
    ``YYYY-MM-DD-HHMMSS``. Networked / mobile backups print a date
    differently — we match both shapes."""
    out = _run_tmutil(["latestbackup"]).strip()
    if not out:
        return None
    # Snapshot path (APFS local snapshots): ".../2026-05-10-141522"
    m = re.search(r"(\d{4}-\d{2}-\d{2}-\d{6})", out)
    if m:
        try:
            naive = datetime.strptime(m.group(1), "%Y-%m-%d-%H%M%S")
            local = naive.astimezone()  # attach system tz
            return local.astimezone(UTC).isoformat()
        except ValueError:
            return None
    # Some setups output an ISO-ish date directly.
    m = re.search(r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})", out)
    return m.group(1) if m else None


class TimeMachineTicker(AbstractTicker):
    name = "time_machine"

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
            self._topic_prefix, self._host_slug, f"time_machine/{suffix}"
        )

    def _unique_id(self, suffix: str) -> str:
        return screen_time_unique_id(self._host_slug, "time_machine", suffix)

    async def run_once(self, mqtt: MqttPublisher) -> None:
        status_text = _run_tmutil(["status"])
        running, percent, phase = _parse_status(status_text)
        last_iso = _last_backup_iso()

        mqtt.publish_state(self._state_topic("running"), "ON" if running else "OFF")
        mqtt.publish_state(self._state_topic("progress"), percent if percent is not None else 0)
        mqtt.publish_attributes(
            self._state_topic("running") + "/attrs",
            {"phase": phase or "", "running": running, "percent": percent},
        )
        if last_iso is not None:
            mqtt.publish_state(self._state_topic("last_backup"), last_iso)

        logger.info(
            "time_machine tick: running=%s percent=%s phase=%s last=%s",
            running, percent, phase, last_iso,
        )

    async def publish_discovery(self, mqtt: MqttPublisher) -> None:
        device = mqtt.host_device_block()

        bin_payload: dict[str, Any] = build_discovery_payload(
            name="Time Machine - Backing Up",
            unique_id=self._unique_id("running"),
            state_topic=self._state_topic("running"),
            availability_topic=self._availability,
            device=device,
            icon="mdi:backup-restore",
            entity_category="diagnostic",
            json_attributes_topic=self._state_topic("running") + "/attrs",
        )
        bin_payload["payload_on"] = "ON"
        bin_payload["payload_off"] = "OFF"
        mqtt.publish_discovery(
            component="binary_sensor",
            unique_id=self._unique_id("running"),
            payload=bin_payload,
        )

        mqtt.publish_discovery(
            component="sensor",
            unique_id=self._unique_id("progress"),
            payload=build_discovery_payload(
                name="Time Machine - Progress",
                unique_id=self._unique_id("progress"),
                state_topic=self._state_topic("progress"),
                availability_topic=self._availability,
                device=device,
                state_class="measurement",
                unit_of_measurement="%",
                icon="mdi:progress-clock",
                entity_category="diagnostic",
            ),
        )
        # last_backup is HISTORICAL — keep showing the last successful
        # backup timestamp even when the bridge is offline, so the user
        # can still see "last backup was 2 days ago" without availability
        # masking it.
        mqtt.publish_discovery(
            component="sensor",
            unique_id=self._unique_id("last_backup"),
            payload=build_discovery_payload(
                name="Time Machine - Last Backup",
                unique_id=self._unique_id("last_backup"),
                state_topic=self._state_topic("last_backup"),
                availability_topic=None,
                device=device,
                device_class="timestamp",
                icon="mdi:clock-check-outline",
                entity_category="diagnostic",
            ),
        )
