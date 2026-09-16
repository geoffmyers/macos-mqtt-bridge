"""Shared utilities for MQTT → Home Assistant bridges.

Pure-data helpers (discovery payload builders, topic helpers, ISO-8601
time helpers, env-var substitution) PLUS I/O helpers:

  - ``Outbox`` — disk-backed JSONL FIFO queue for bridges that need to
    survive broker outages / process restarts without dropping events.
  - ``ThreadedPublisher`` — paho-mqtt v5 wrapper with LWT, subscribe
    map, and the publish flavors the threaded bridges need. Subclass it
    to add bridge-specific behavior (e.g. a host device block).
  - ``mqtt_client_kwargs`` — aiomqtt construction-kwargs builder for
    asyncio bridges.
  - ``load_yaml_with_env`` / ``substitute_env_vars`` — config helpers
    for bridges that use the YAML-with-``${ENV_VAR}`` pattern.

Bridges typically import from the package root for the pieces they use.
The aiomqtt + paho code paths coexist; importing this package will pull
in ``paho-mqtt`` (required) but not ``aiomqtt`` (optional, asyncio
bridges) or ``pyyaml`` (optional, YAML config) — those are imported lazily
where relevant.
"""

from ha_mqtt_bridge.aiomqtt_helpers import mqtt_client_kwargs
from ha_mqtt_bridge.app_helpers import (
    configure_logging,
    register_github_error_reporter,
)
from ha_mqtt_bridge.config_helpers import load_yaml_with_env, substitute_env_vars
from ha_mqtt_bridge.discovery import (
    availability_block,
    build_device_block,
    build_discovery_payload,
)
from ha_mqtt_bridge.outbox import Outbox
from ha_mqtt_bridge.paho_publisher import MessageHandler, ThreadedPublisher
from ha_mqtt_bridge.time_utils import (
    epoch_ms_to_iso,
    epoch_to_iso,
    iso_from_monotonic,
    iso_now,
    now_ms,
    now_s,
)
from ha_mqtt_bridge.topics import (
    discovery_topic,
    event_topic,
    slugify,
    slugify_hostname,
    state_topic,
)

__all__ = [
    "MessageHandler",
    "Outbox",
    "ThreadedPublisher",
    "availability_block",
    "build_device_block",
    "build_discovery_payload",
    "configure_logging",
    "discovery_topic",
    "epoch_ms_to_iso",
    "epoch_to_iso",
    "event_topic",
    "iso_from_monotonic",
    "iso_now",
    "load_yaml_with_env",
    "mqtt_client_kwargs",
    "now_ms",
    "now_s",
    "register_github_error_reporter",
    "slugify",
    "slugify_hostname",
    "state_topic",
    "substitute_env_vars",
]

__version__ = "0.5.0"
