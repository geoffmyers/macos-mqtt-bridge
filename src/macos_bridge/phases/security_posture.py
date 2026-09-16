"""macOS security posture: FileVault, Application Firewall, SIP.

Three slow-changing system flags. We expose them as a single sensor
whose state is a comma-joined list of *enabled* protections (mirrors the
``permissions`` ticker shape) and a binary_sensor per component for HA
automations that key off a specific protection.

Publishes:

  sensor.<host>_security_enabled              — comma list of enabled
  attrs.* {filevault, firewall, sip}          — per-component status
  binary_sensor.<host>_security_filevault     — ON when FileVault is on
  binary_sensor.<host>_security_firewall      — ON when ALF is on
  binary_sensor.<host>_security_sip           — ON when SIP is enabled
"""

from __future__ import annotations

import logging
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


def _probe_filevault(timeout: float = 3.0) -> bool | None:
    """Return True/False, or None if unreadable."""
    try:
        result = subprocess.run(
            ["fdesetup", "status"],
            capture_output=True, text=True, timeout=timeout, check=False,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
        logger.debug("fdesetup failed: %s", exc)
        return None
    out = (result.stdout or "").lower()
    if "filevault is on" in out:
        return True
    if "filevault is off" in out:
        return False
    return None


def _probe_firewall(timeout: float = 3.0) -> bool | None:
    """Application Layer Firewall global state lives in defaults under
    ``/Library/Preferences/com.apple.alf:globalstate``. 0 = off,
    1 = on, 2 = block all incoming."""
    try:
        result = subprocess.run(
            ["defaults", "read", "/Library/Preferences/com.apple.alf", "globalstate"],
            capture_output=True, text=True, timeout=timeout, check=False,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None
    out = result.stdout.strip()
    if not out or result.returncode != 0:
        return None
    try:
        return int(out) >= 1
    except ValueError:
        return None


def _probe_sip(timeout: float = 3.0) -> bool | None:
    try:
        result = subprocess.run(
            ["csrutil", "status"],
            capture_output=True, text=True, timeout=timeout, check=False,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None
    out = (result.stdout or "").lower()
    if "enabled" in out and "disabled" not in out:
        return True
    if "disabled" in out:
        return False
    return None


class SecurityPostureTicker(AbstractTicker):
    name = "security_posture"

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

    def _state_topic(self, suffix: str = "enabled") -> str:
        return screen_time_state_topic(
            self._topic_prefix, self._host_slug, f"security/{suffix}"
        )

    def _unique_id(self, suffix: str) -> str:
        return screen_time_unique_id(self._host_slug, "security", suffix)

    async def run_once(self, mqtt: MqttPublisher) -> None:
        fv = _probe_filevault()
        fw = _probe_firewall()
        sip = _probe_sip()

        labels = []
        if fv:
            labels.append("FileVault")
        if fw:
            labels.append("Firewall")
        if sip:
            labels.append("SIP")
        state = ", ".join(labels) if labels else "None"

        mqtt.publish_state(self._state_topic("enabled"), state)
        mqtt.publish_state(self._state_topic("filevault"), _bool_payload(fv))
        mqtt.publish_state(self._state_topic("firewall"), _bool_payload(fw))
        mqtt.publish_state(self._state_topic("sip"), _bool_payload(sip))
        mqtt.publish_attributes(
            self._state_topic("enabled") + "/attrs",
            {
                "filevault": _tri_state(fv),
                "firewall": _tri_state(fw),
                "sip": _tri_state(sip),
                "enabled_count": len(labels),
                "enabled": labels,
                "checked_at": datetime.now(tz=UTC).isoformat(),
            },
        )
        logger.info(
            "security_posture tick: enabled=%d/3 — filevault=%s firewall=%s sip=%s",
            len(labels), _tri_state(fv), _tri_state(fw), _tri_state(sip),
        )

    async def publish_discovery(self, mqtt: MqttPublisher) -> None:
        device = mqtt.host_device_block()

        mqtt.publish_discovery(
            component="sensor",
            unique_id=self._unique_id("enabled"),
            payload=build_discovery_payload(
                name="Security Posture",
                unique_id=self._unique_id("enabled"),
                state_topic=self._state_topic("enabled"),
                availability_topic=None,
                device=device,
                icon="mdi:shield-lock",
                entity_category="diagnostic",
                json_attributes_topic=self._state_topic("enabled") + "/attrs",
            ),
        )
        for suffix, name, icon in [
            ("filevault", "FileVault Enabled", "mdi:harddisk-lock"),
            ("firewall", "Firewall Enabled", "mdi:wall"),
            ("sip", "SIP Enabled", "mdi:shield-key"),
        ]:
            payload: dict[str, Any] = build_discovery_payload(
                name=name,
                unique_id=self._unique_id(suffix),
                state_topic=self._state_topic(suffix),
                availability_topic=None,
                device=device,
                icon=icon,
                entity_category="diagnostic",
            )
            payload["payload_on"] = "ON"
            payload["payload_off"] = "OFF"
            mqtt.publish_discovery(
                component="binary_sensor",
                unique_id=self._unique_id(suffix),
                payload=payload,
            )


def _bool_payload(value: bool | None) -> str:
    """Tri-state to ``ON`` / ``OFF`` / ``unknown`` for binary_sensor topics.
    HA renders an unknown value as the entity going to ``unknown`` state."""
    if value is None:
        return "unknown"
    return "ON" if value else "OFF"


def _tri_state(value: bool | None) -> str:
    if value is None:
        return "unknown"
    return "enabled" if value else "disabled"
