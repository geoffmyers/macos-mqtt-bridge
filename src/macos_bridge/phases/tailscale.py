"""Tailscale connectivity sensor.

Parses ``tailscale status --json`` to surface the local node's state, IP
addresses, peer count, and exit-node usage. Skips silently if the
``tailscale`` CLI isn't on PATH (so this ticker is safe to leave enabled
on Macs that don't have Tailscale).

Publishes:

  sensor.<host>_tailscale_state            — Running / Stopped / NoState / NeedsLogin
  sensor.<host>_tailscale_ip               — first IPv4 from TailscaleIPs
  sensor.<host>_tailscale_peers_online     — count of online peers
  binary_sensor.<host>_tailscale_exit_node — ON when an exit node is in use
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
from typing import Any

from macos_bridge.mqtt import MqttPublisher, build_discovery_payload
from macos_bridge.phases.base import (
    AbstractTicker,
    screen_time_state_topic,
    screen_time_unique_id,
)

logger = logging.getLogger(__name__)


def _run_tailscale_status(timeout: float = 5.0) -> dict | None:
    """Return the parsed JSON status, or None if unavailable / failed."""
    if not shutil.which("tailscale"):
        return None
    try:
        result = subprocess.run(
            ["tailscale", "status", "--json"],
            capture_output=True, text=True, timeout=timeout, check=False,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
        logger.debug("tailscale failed: %s", exc)
        return None
    if result.returncode != 0:
        return None
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        return None


def _first_ipv4(ips: list[str]) -> str | None:
    for ip in ips or []:
        if "." in ip and ":" not in ip:
            return ip
    return None


class TailscaleTicker(AbstractTicker):
    name = "tailscale"

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
            self._topic_prefix, self._host_slug, f"tailscale/{suffix}"
        )

    def _unique_id(self, suffix: str) -> str:
        return screen_time_unique_id(self._host_slug, "tailscale", suffix)

    async def run_once(self, mqtt: MqttPublisher) -> None:
        status = _run_tailscale_status()
        if status is None:
            mqtt.publish_state(self._state_topic("state"), "Unavailable")
            mqtt.publish_state(self._state_topic("ip"), "")
            mqtt.publish_state(self._state_topic("peers_online"), 0)
            mqtt.publish_state(self._state_topic("exit_node"), "OFF")
            return

        backend_state = str(status.get("BackendState") or "Unknown")
        self_node = status.get("Self") or {}
        ip = _first_ipv4(self_node.get("TailscaleIPs") or []) or ""

        peers = status.get("Peer") or {}
        online_peers = sum(
            1 for p in peers.values()
            if isinstance(p, dict) and p.get("Online")
        )
        exit_node_in_use = any(
            isinstance(p, dict) and p.get("ExitNode") for p in peers.values()
        )

        mqtt.publish_state(self._state_topic("state"), backend_state)
        mqtt.publish_state(self._state_topic("ip"), ip)
        mqtt.publish_state(self._state_topic("peers_online"), online_peers)
        mqtt.publish_state(
            self._state_topic("exit_node"), "ON" if exit_node_in_use else "OFF"
        )
        mqtt.publish_attributes(
            self._state_topic("state") + "/attrs",
            {
                "tailscale_ips": self_node.get("TailscaleIPs") or [],
                "hostname": self_node.get("HostName") or "",
                "dns_name": self_node.get("DNSName") or "",
                "version": status.get("Version") or "",
                "peer_total": len(peers),
                "peer_online": online_peers,
            },
        )
        logger.info(
            "tailscale tick: state=%s ip=%s peers=%d/%d exit=%s",
            backend_state, ip, online_peers, len(peers), exit_node_in_use,
        )

    async def publish_discovery(self, mqtt: MqttPublisher) -> None:
        device = mqtt.host_device_block()

        sensors = [
            ("state", "Tailscale - State", "mdi:vpn", None, None, "attrs"),
            ("ip", "Tailscale - IP", "mdi:ip-network", None, None, None),
            ("peers_online", "Tailscale - Peers Online",
             "mdi:lan-connect", "measurement", "count", None),
        ]
        for suffix, name, icon, state_class, unit, attrs_suffix in sensors:
            mqtt.publish_discovery(
                component="sensor",
                unique_id=self._unique_id(suffix),
                payload=build_discovery_payload(
                    name=name,
                    unique_id=self._unique_id(suffix),
                    state_topic=self._state_topic(suffix),
                    availability_topic=self._availability,
                    device=device,
                    state_class=state_class,
                    unit_of_measurement=unit,
                    icon=icon,
                    json_attributes_topic=(
                        self._state_topic(suffix) + "/attrs" if attrs_suffix else None
                    ),
                ),
            )

        bin_payload: dict[str, Any] = build_discovery_payload(
            name="Tailscale - Exit Node In Use",
            unique_id=self._unique_id("exit_node"),
            state_topic=self._state_topic("exit_node"),
            availability_topic=self._availability,
            device=device,
            icon="mdi:exit-run",
        )
        bin_payload["payload_on"] = "ON"
        bin_payload["payload_off"] = "OFF"
        mqtt.publish_discovery(
            component="binary_sensor",
            unique_id=self._unique_id("exit_node"),
            payload=bin_payload,
        )
