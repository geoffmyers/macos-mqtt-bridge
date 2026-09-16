"""Threaded paho-mqtt v5 publisher with LWT, subscribe map, and the
publish flavors paho-based bridges need.

Extracted from one bridge's publisher so every paho-based bridge can
reuse the same connection / LWT / publish semantics instead of
hand-rolling them.

Bridges that need bridge-specific behavior (e.g. an HA device-block
factory tied to the local host's identity) should subclass
``ThreadedPublisher`` and add it as a method or property (a
``host_device_block()``, for example).

Semantics:

  - Persistent connection with last-will (LWT) on a configurable
    topic; publishes ``lwt_online`` on connect and ``lwt_offline``
    via paho's will_set on disconnect. Both retained.
  - Topic subscriptions tracked in a per-topic message-handler dict;
    re-applied automatically on reconnect.
  - Five publish flavors:
      * ``publish_event(topic, payload, retain=None)`` — JSON,
        fire-and-forget. ``retain`` defaults to the publisher's
        ``retain_events`` setting (typically False).
      * ``publish_event_with_ack(topic, payload, retain=None,
        timeout)`` -> bool — JSON, blocks on broker ACK. Returns False
        on timeout, disconnect, or paho error. Use this in outbox
        drain loops where False triggers a retry.
      * ``publish_state(topic, value)`` — string scalar; retains by
        default. Used by state-mirror tickers.
      * ``publish_attributes(topic, dict)`` — JSON dict; retains by
        default. Used for HA ``json_attributes_topic`` companion
        publishes.
      * ``publish_discovery(component, unique_id, payload)`` —
        JSON, ALWAYS retained, on the configured discovery_prefix.
      * ``publish_raw(topic, payload, qos=0, retain=False)`` — escape
        hatch for clearing retained configs with empty payloads.
"""

from __future__ import annotations

import json
import logging
import os
import ssl
import threading
import time
from collections.abc import Callable, Mapping
from typing import Any

import paho.mqtt.client as paho

log = logging.getLogger(__name__)

MessageHandler = Callable[[str, bytes], None]


class ThreadedPublisher:
    """Paho-mqtt v5 wrapper. Thread-safe enough for the bridges' needs
    (publish is paho's responsibility; subscribe map is mutated only
    from the main thread and the on_connect callback)."""

    def __init__(
        self,
        *,
        host: str,
        port: int = 1883,
        username: str | None = None,
        password: str | None = None,
        client_id: str | None = None,
        tls: bool = False,
        ca_file: str | None = None,
        keepalive: int = 60,
        lwt_topic: str | None = None,
        lwt_online: str = "online",
        lwt_offline: str = "offline",
        event_qos: int = 1,
        state_qos: int = 0,
        discovery_qos: int = 1,
        retain_events: bool = False,
        retain_state: bool = True,
        discovery_prefix: str = "homeassistant",
        health_path: str | None = None,
    ) -> None:
        self.host = host
        self.port = port
        self.tls = tls
        self.keepalive = keepalive
        self.lwt_topic = lwt_topic
        self.lwt_online = lwt_online
        self.lwt_offline = lwt_offline
        self.event_qos = event_qos
        self.state_qos = state_qos
        self.discovery_qos = discovery_qos
        self.retain_events = retain_events
        self.retain_state = retain_state
        self.discovery_prefix = discovery_prefix
        # Liveness heartbeat: every successful publish bumps the mtime of
        # this file. A Docker healthcheck (or any external supervisor) can
        # stat the file and consider the bridge unhealthy if it hasn't
        # been touched recently. None disables; the Docker bridges use
        # "/tmp/healthy".
        self.health_path = health_path

        self._connected = threading.Event()
        self._subscriptions: dict[str, MessageHandler] = {}

        self._client = paho.Client(
            paho.CallbackAPIVersion.VERSION2,
            client_id=client_id,
            protocol=paho.MQTTv5,
        )
        self._client.on_connect = self._on_connect
        self._client.on_disconnect = self._on_disconnect
        self._client.on_message = self._on_message

        if username:
            self._client.username_pw_set(username, password or "")

        if tls:
            self._client.tls_set(
                ca_certs=ca_file,
                cert_reqs=ssl.CERT_REQUIRED,
                tls_version=ssl.PROTOCOL_TLS_CLIENT,
            )

        if lwt_topic:
            self._client.will_set(
                lwt_topic,
                payload=lwt_offline,
                qos=event_qos,
                retain=True,
            )

    # ---- lifecycle ---------------------------------------------------------------

    def start(self) -> None:
        log.info(
            "connecting MQTT to %s:%d (tls=%s)", self.host, self.port, self.tls,
        )
        self._client.connect_async(self.host, self.port, keepalive=self.keepalive)
        self._client.loop_start()

    def stop(self) -> None:
        if self.lwt_topic:
            try:
                self._client.publish(
                    self.lwt_topic,
                    payload=self.lwt_offline,
                    qos=self.event_qos,
                    retain=True,
                ).wait_for_publish(timeout=2.0)
            except Exception:  # noqa: BLE001
                pass
        self._client.loop_stop()
        self._client.disconnect()

    def wait_until_connected(self, timeout: float = 10.0) -> bool:
        return self._connected.wait(timeout)

    def is_connected(self) -> bool:
        return self._connected.is_set()

    # ---- liveness heartbeat ------------------------------------------------------

    def _touch_health(self) -> None:
        path = self.health_path
        if not path:
            return
        try:
            # os.utime on a missing file raises FileNotFoundError; create
            # then set mtime in one syscall via open+close + utime, or use
            # Path.touch which handles both. Keep it as a plain syscall pair
            # to avoid importing pathlib in the hot publish path.
            with open(path, "a"):
                pass
            now = time.time()
            os.utime(path, (now, now))
        except OSError:
            # Disk full / permission denied / read-only FS — never let the
            # heartbeat crash a publish.
            log.debug("health-path touch failed", exc_info=True)

    # ---- subscriptions -----------------------------------------------------------

    def subscribe(self, topic: str, handler: MessageHandler) -> None:
        self._subscriptions[topic] = handler
        if self._connected.is_set():
            self._client.subscribe(topic, qos=self.event_qos)

    # ---- publish — events --------------------------------------------------------

    def publish_event(
        self,
        topic: str,
        payload: Mapping[str, Any],
        *,
        retain: bool | None = None,
    ) -> None:
        body = json.dumps(payload, separators=(",", ":"), default=str)
        self._client.publish(
            topic,
            payload=body,
            qos=self.event_qos,
            retain=self.retain_events if retain is None else retain,
        )
        self._touch_health()

    def publish_event_with_ack(
        self,
        topic: str,
        payload: Mapping[str, Any],
        *,
        retain: bool | None = None,
        timeout: float = 5.0,
    ) -> bool:
        if not self._connected.is_set():
            return False
        body = json.dumps(payload, separators=(",", ":"), default=str)
        info = self._client.publish(
            topic,
            payload=body,
            qos=self.event_qos,
            retain=self.retain_events if retain is None else retain,
        )
        try:
            info.wait_for_publish(timeout=timeout)
        except (RuntimeError, ValueError):
            return False
        if info.is_published():
            self._touch_health()
            return True
        return False

    # ---- publish — state-mirror --------------------------------------------------

    def publish_state(self, topic: str, value: str | int | float) -> None:
        self._client.publish(
            topic,
            str(value),
            qos=self.state_qos,
            retain=self.retain_state,
        )
        self._touch_health()

    def publish_attributes(
        self, topic: str, payload: Mapping[str, Any]
    ) -> None:
        self._client.publish(
            topic,
            json.dumps(payload, sort_keys=True, default=str),
            qos=self.state_qos,
            retain=self.retain_state,
        )
        self._touch_health()

    # ---- publish — HA discovery (one-shot retained at startup) ------------------

    def publish_discovery(
        self,
        *,
        component: str,
        unique_id: str,
        payload: dict[str, Any],
    ) -> None:
        topic = f"{self.discovery_prefix}/{component}/{unique_id}/config"
        self._client.publish(
            topic,
            json.dumps(payload, sort_keys=True, default=str),
            qos=self.discovery_qos,
            retain=True,
        )
        self._touch_health()

    def publish_raw(
        self,
        topic: str,
        payload: str | bytes,
        *,
        qos: int = 0,
        retain: bool = False,
    ) -> None:
        """Escape hatch — used for clearing stale retained discovery
        configs with empty payloads. Avoid for ordinary publishes."""
        self._client.publish(topic, payload, qos=qos, retain=retain)
        self._touch_health()

    # ---- callbacks ---------------------------------------------------------------

    def _on_message(self, _client, _userdata, msg) -> None:
        handler = self._subscriptions.get(msg.topic)
        if handler is None:
            log.debug("ignoring message on unsubscribed topic %s", msg.topic)
            return
        try:
            handler(msg.topic, msg.payload)
        except Exception:  # noqa: BLE001
            log.exception("handler raised for topic %s", msg.topic)

    def _on_connect(
        self, _client, _userdata, _flags, reason_code, _properties=None
    ) -> None:
        rc = getattr(reason_code, "value", reason_code)
        if rc == 0:
            log.info("MQTT connected")
            self._connected.set()
            if self.lwt_topic:
                self._client.publish(
                    self.lwt_topic,
                    payload=self.lwt_online,
                    qos=self.event_qos,
                    retain=True,
                )
            for topic in self._subscriptions:
                self._client.subscribe(topic, qos=self.event_qos)
        else:
            log.error("MQTT connect failed: %s", reason_code)

    def _on_disconnect(
        self, _client, _userdata, _flags, reason_code, _properties=None
    ) -> None:
        log.warning("MQTT disconnected: %s", reason_code)
        self._connected.clear()
