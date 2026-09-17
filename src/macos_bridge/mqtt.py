"""macOS MQTT publisher.

Thin subclass of ``ha_mqtt_bridge.ThreadedPublisher`` that:

  - Resolves MQTT credentials from environment variables named by the
    config (``MQTT_USERNAME`` / ``MQTT_PASSWORD`` by default).
  - Stores macOS-specific identity (host_slug, sw_version, serial
    number, en0 MAC, friendly name) and exposes ``host_device_block()``
    so every sensor from every phase ticker / event source groups
    under the same HA device.

The bulk of the wire-level behavior — persistent connection, LWT,
subscribe map, publish flavors — lives in the toolkit's
``ThreadedPublisher``. See its docstring for the full publish surface
the bridges share.

``build_discovery_payload`` re-exports the toolkit's
identically-shaped builder behind a macOS-specific signature so
existing call sites in ``phases/`` don't have to change.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from typing import Any

from ha_mqtt_bridge import build_device_block
from ha_mqtt_bridge import build_discovery_payload as _build_discovery_payload
from ha_mqtt_bridge.paho_publisher import MessageHandler as _MessageHandler
from ha_mqtt_bridge.paho_publisher import ThreadedPublisher

from macos_bridge.config import MqttConfig

log = logging.getLogger(__name__)

MessageHandler = _MessageHandler  # re-export for backwards compatibility


def build_discovery_payload(
    *,
    name: str,
    unique_id: str,
    state_topic: str,
    availability_topic: str | None,
    device: dict[str, Any],
    device_class: str | None = None,
    unit_of_measurement: str | None = None,
    state_class: str | None = None,
    icon: str | None = None,
    json_attributes_topic: str | None = None,
    object_id: str | None = None,
    entity_category: str | None = None,
) -> dict[str, Any]:
    """Build a Home Assistant MQTT discovery config payload.

    Thin macOS-flavored wrapper around
    ``ha_mqtt_bridge.build_discovery_payload`` that preserves this
    package's keyword-only signature (``availability_topic`` as a
    required arg even when ``None``) so existing call sites in
    ``phases/`` don't need to be touched.

    Pass ``availability_topic=None`` for historical sensors that should
    keep displaying their last retained value in HA even when the bridge
    is offline (e.g. last_message_received, last_phone_call, software
    update count). Live-state sensors (focused app, system stats, virtual
    meeting in_progress, etc.) should pass the LWT topic so HA flips
    them to "Unavailable" when the bridge disconnects — that signals
    "this value is stale" rather than "this value is current".
    """
    return _build_discovery_payload(
        name=name,
        unique_id=unique_id,
        state_topic=state_topic,
        device=device,
        object_id=object_id,
        device_class=device_class,
        unit_of_measurement=unit_of_measurement,
        state_class=state_class,
        icon=icon,
        json_attributes_topic=json_attributes_topic,
        entity_category=entity_category,
        availability_topic=availability_topic,
    )


class MqttPublisher(ThreadedPublisher):
    """Threaded MQTT publisher with macOS-specific HA device identity.

    ``MqttConfig`` is the source of truth for connection / topic /
    QoS / retain settings; this constructor unpacks those into the
    ``ThreadedPublisher`` keyword args and adds macOS identity fields
    (serial number, en0 MAC, friendly name).
    """

    def __init__(
        self,
        cfg: MqttConfig,
        *,
        host_slug: str,
        sw_version: str,
        host_friendly_name: str | None = None,
        serial_number: str | None = None,
        mac_address: str | None = None,
    ) -> None:
        username = os.environ.get(cfg.username_env)
        password = os.environ.get(cfg.password_env)
        super().__init__(
            host=cfg.host,
            port=cfg.port,
            username=username,
            password=password,
            client_id=cfg.client_id,
            tls=cfg.tls,
            ca_file=cfg.ca_file,
            keepalive=cfg.keepalive,
            lwt_topic=cfg.lwt_topic,
            lwt_online=cfg.lwt_online,
            lwt_offline=cfg.lwt_offline,
            event_qos=cfg.event_qos,
            state_qos=cfg.state_qos,
            discovery_qos=cfg.discovery_qos,
            retain_events=cfg.retain_events,
            retain_state=cfg.retain_state,
            discovery_prefix=cfg.discovery_prefix,
        )
        self.cfg = cfg
        self._host_slug = host_slug
        self._sw_version = sw_version
        self._host_friendly_name = host_friendly_name
        self._serial_number = serial_number
        self._mac_address = mac_address

    # ---- identity / device block ---------------------------------------------------

    @property
    def host_slug(self) -> str:
        return self._host_slug

    @property
    def sw_version(self) -> str:
        return self._sw_version

    @property
    def host_friendly_name(self) -> str | None:
        return self._host_friendly_name

    @property
    def serial_number(self) -> str | None:
        return self._serial_number

    @property
    def mac_address(self) -> str | None:
        return self._mac_address

    def host_device_block(self) -> dict[str, Any]:
        """Per-Mac HA device block grouping all sensors from this bridge.

        Includes ``apple-serial:<serial>`` as a secondary identifier and
        ``connections=[["mac", <en0-mac>]]`` so HA can deduplicate against
        any other integration that already knows this Mac by hardware.
        """
        display = self._host_friendly_name or self._host_slug
        identifiers = [f"macos-mqtt-bridge:{self._host_slug}"]
        if self._serial_number:
            identifiers.append(f"apple-serial:{self._serial_number}")
        return build_device_block(
            identifiers=identifiers,
            name=f"macOS Bridge — {display}",
            manufacturer="Apple",
            model="macos-mqtt-bridge",
            sw_version=self._sw_version,
            serial_number=self._serial_number,
            connections=(
                [["mac", self._mac_address]] if self._mac_address else None
            ),
        )
