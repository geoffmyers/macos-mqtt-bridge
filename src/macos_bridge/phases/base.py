"""Phase ticker interface."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from macos_bridge.mqtt import MqttPublisher


class AbstractTicker(ABC):
    """Each phase implements this. Runtime instantiates one per enabled phase."""

    name: str

    @property
    @abstractmethod
    def enabled(self) -> bool: ...

    @property
    @abstractmethod
    def interval_seconds(self) -> int | None:
        """Fixed interval between ticks. Return None for self-paced loops (Phase C)."""

    @abstractmethod
    async def run_once(self, mqtt: MqttPublisher) -> None:
        """Execute one tick: query DB, publish state."""

    async def publish_discovery(self, mqtt: MqttPublisher) -> None:
        """Publish HA discovery configs for all entities this phase owns.
        Default no-op; phases override if they have discovery payloads.
        """
        return None


# Helpers shared across phases for the merged topic + unique_id scheme.
#
# Phase tickers publish at the top level under ``<prefix>/<host>/...``.
# The ``comms/`` sub-prefix is reserved for per-event comms-source topics
# (Messages / Phone / FaceTime / Voicemail), so phase topics never collide
# with comms event topics.

def screen_time_state_topic(topic_prefix: str, host_slug: str, suffix: str) -> str:
    """``<topic_prefix>/<host>/<suffix>``"""
    return f"{topic_prefix}/{host_slug}/{suffix}"


def availability_topic(lwt_topic: str) -> str:
    return lwt_topic


def screen_time_unique_id(host_slug: str, *parts: str) -> str:
    """``macos_<host>_<part1>_<part2>...``"""
    return "_".join(["macos", host_slug, *parts])
