"""Unread iMessage / SMS counter.

Reuses the chat.db database the comms event source already polls. Counts
incoming-only messages with ``is_read = 0`` and exposes both a total
and a per-chat breakdown via attrs.

Publishes:

  sensor.<host>_unread_messages_count          — total unread count
  attrs.unread_by_chat                         — list of {chat, count}
                                                 ordered by recency
"""

from __future__ import annotations

import logging
from typing import Any

from macos_bridge.config import MessagesSource as MessagesConfig
from macos_bridge.db import open_ro
from macos_bridge.mqtt import MqttPublisher, build_discovery_payload
from macos_bridge.phases.base import (
    AbstractTicker,
    screen_time_state_topic,
    screen_time_unique_id,
)

logger = logging.getLogger(__name__)

_TOTAL_QUERY = """
    SELECT COUNT(*) AS n
    FROM message
    WHERE is_from_me = 0
      AND (is_read IS NULL OR is_read = 0)
"""

_PER_CHAT_QUERY = """
    SELECT
        COALESCE(c.display_name, c.guid, '?') AS chat,
        COUNT(*) AS n,
        MAX(m.date) AS latest_date
    FROM message m
    LEFT JOIN chat_message_join cmj ON m.ROWID = cmj.message_id
    LEFT JOIN chat c ON cmj.chat_id = c.ROWID
    WHERE m.is_from_me = 0
      AND (m.is_read IS NULL OR m.is_read = 0)
    GROUP BY c.ROWID
    ORDER BY latest_date DESC
    LIMIT 20
"""


class UnreadMessagesTicker(AbstractTicker):
    name = "unread_messages"

    def __init__(
        self,
        *,
        enabled: bool,
        interval_seconds: int,
        host_slug: str,
        topic_prefix: str,
        availability_topic: str,
        messages_db_path: str | None,
        messages_cfg: MessagesConfig,
    ) -> None:
        self._enabled = enabled
        self._interval_seconds = interval_seconds
        self._host_slug = host_slug
        self._topic_prefix = topic_prefix
        self._availability = availability_topic
        self._messages_db_path = messages_db_path
        self._messages_cfg = messages_cfg

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def interval_seconds(self) -> int | None:
        return self._interval_seconds

    def _state_topic(self, suffix: str = "count") -> str:
        return screen_time_state_topic(
            self._topic_prefix, self._host_slug, f"unread_messages/{suffix}"
        )

    def _unique_id(self) -> str:
        return screen_time_unique_id(self._host_slug, "unread_messages")

    def _query(self) -> tuple[int, list[dict[str, Any]]]:
        if not self._messages_db_path or not self._messages_cfg.enabled:
            return 0, []
        try:
            with open_ro(self._messages_db_path) as conn:
                total_row = conn.execute(_TOTAL_QUERY).fetchone()
                per_chat_rows = conn.execute(_PER_CHAT_QUERY).fetchall()
        except FileNotFoundError:
            return 0, []
        except Exception:
            logger.exception("unread_messages: query failed")
            return 0, []

        total = int(total_row["n"] or 0)
        per_chat = [
            {"chat": str(r["chat"]), "count": int(r["n"])}
            for r in per_chat_rows
        ]
        return total, per_chat

    async def run_once(self, mqtt: MqttPublisher) -> None:
        total, per_chat = self._query()
        mqtt.publish_state(self._state_topic("count"), total)
        mqtt.publish_attributes(
            self._state_topic("count") + "/attrs",
            {"by_chat": per_chat, "by_chat_count": len(per_chat)},
        )
        logger.info(
            "unread_messages tick: total=%d across %d chat(s)",
            total, len(per_chat),
        )

    async def publish_discovery(self, mqtt: MqttPublisher) -> None:
        device = mqtt.host_device_block()
        mqtt.publish_discovery(
            component="sensor",
            unique_id=self._unique_id(),
            payload=build_discovery_payload(
                name="Messages - Unread",
                unique_id=self._unique_id(),
                state_topic=self._state_topic("count"),
                availability_topic=None,
                device=device,
                state_class="measurement",
                unit_of_measurement="count",
                icon="mdi:message-badge",
                json_attributes_topic=self._state_topic("count") + "/attrs",
            ),
        )
