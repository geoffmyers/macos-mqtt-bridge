"""Today-so-far comms counters.

Periodically counts new messages, calls, and voicemails since the local
start-of-today and publishes five retained sensors:

  sensor.<host>_today_messages_received   — count(*) where is_from_me=0
  sensor.<host>_today_messages_sent       — count(*) where is_from_me=1
  sensor.<host>_today_phone_calls         — count(*) phone records
  sensor.<host>_today_facetime_calls      — count(*) FaceTime records
  sensor.<host>_today_voicemails          — count(*) phone voicemails

Counters reset when the wall clock crosses local midnight (we always
query against ``start_of_today_mac_absolute_time`` at tick time).

Reads from the same SQLite databases as the comms event sources.
``chat.db.message.date`` is in nanoseconds (post-El-Capitan); the call
and voicemail dates are seconds.
"""

from __future__ import annotations

import logging
from typing import Any
from zoneinfo import ZoneInfo

from macos_bridge.config import (
    CallsSource as CallsConfig,
    MessagesSource as MessagesConfig,
    VoicemailSource as VoicemailConfig,
)
from macos_bridge.db import open_ro
from macos_bridge.mqtt import MqttPublisher, build_discovery_payload
from macos_bridge.phases.base import (
    AbstractTicker,
    screen_time_state_topic,
    screen_time_unique_id,
)
from macos_bridge.time_utils import start_of_today_mac_absolute_time

logger = logging.getLogger(__name__)

# `message.date` on chat.db is nanoseconds since the Mac epoch. Pass the
# threshold AS A NUMBER so SQLite's > comparison stays numeric (vs string
# lexicographic compare, which would silently mis-rank dates).
_MESSAGES_COUNT_QUERY = """
    SELECT
        SUM(CASE WHEN is_from_me = 0 THEN 1 ELSE 0 END) AS received,
        SUM(CASE WHEN is_from_me = 1 THEN 1 ELSE 0 END) AS sent
    FROM message
    WHERE date >= ?
"""

_CALLS_COUNT_QUERY = """
    SELECT ZSERVICE_PROVIDER AS service, COUNT(*) AS n
    FROM ZCALLRECORD
    WHERE ZDATE >= ?
    GROUP BY ZSERVICE_PROVIDER
"""

# Same MAILBOXTYPE filter as the live source: 2 = trash, exclude unless
# include_deleted is set in the voicemail config.
_VOICEMAILS_COUNT_QUERY = """
    SELECT ZPROVIDER AS provider, COUNT(*) AS n
    FROM ZSTOREDMESSAGE
    WHERE ZDATECREATED >= ?
      AND (ZMAILBOXTYPE IS NULL OR ZMAILBOXTYPE != 2 OR ? = 1)
    GROUP BY ZPROVIDER
"""


def _classify_call(service: str, calls_cfg: CallsConfig) -> str | None:
    s = (service or "").lower()
    for needle in calls_cfg.facetime_service_substrings:
        if needle.lower() in s:
            return "facetime"
    for needle in calls_cfg.phone_service_substrings:
        if needle.lower() in s:
            return "phone"
    return None


def _classify_provider(provider: str, voicemail_cfg: VoicemailConfig) -> str | None:
    s = (provider or "").lower()
    for needle in voicemail_cfg.facetime_provider_substrings:
        if needle.lower() in s:
            return "facetime"
    for needle in voicemail_cfg.phone_provider_substrings:
        if needle.lower() in s:
            return "phone"
    return None


class TodayCountersTicker(AbstractTicker):
    name = "today_counters"

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
        calls_db_path: str | None,
        calls_cfg: CallsConfig,
        voicemail_db_path: str | None,
        voicemail_cfg: VoicemailConfig,
        local_timezone: ZoneInfo | None = None,
    ) -> None:
        self._enabled = enabled
        self._interval_seconds = interval_seconds
        self._host_slug = host_slug
        self._topic_prefix = topic_prefix
        self._availability = availability_topic
        self._messages_db_path = messages_db_path
        self._messages_cfg = messages_cfg
        self._calls_db_path = calls_db_path
        self._calls_cfg = calls_cfg
        self._voicemail_db_path = voicemail_db_path
        self._voicemail_cfg = voicemail_cfg
        self._tz = local_timezone

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def interval_seconds(self) -> int | None:
        return self._interval_seconds

    def _state_topic(self, suffix: str) -> str:
        return screen_time_state_topic(
            self._topic_prefix, self._host_slug, f"today/{suffix}"
        )

    def _unique_id(self, suffix: str) -> str:
        return screen_time_unique_id(self._host_slug, "today", suffix)

    # ---- counters ----

    def _count_messages(self, since_mat: float) -> tuple[int, int]:
        if not self._messages_db_path or not self._messages_cfg.enabled:
            return 0, 0
        # chat.db dates are in nanoseconds.
        since_ns = int(since_mat * 1_000_000_000)
        try:
            with open_ro(self._messages_db_path) as conn:
                row = conn.execute(_MESSAGES_COUNT_QUERY, (since_ns,)).fetchone()
        except FileNotFoundError:
            return 0, 0
        except Exception:
            logger.exception("today_counters: messages count failed")
            return 0, 0
        return int(row["received"] or 0), int(row["sent"] or 0)

    def _count_calls(self, since_mat: float) -> tuple[int, int]:
        if not self._calls_db_path or not self._calls_cfg.enabled:
            return 0, 0
        try:
            with open_ro(self._calls_db_path) as conn:
                rows = conn.execute(_CALLS_COUNT_QUERY, (since_mat,)).fetchall()
        except FileNotFoundError:
            return 0, 0
        except Exception:
            logger.exception("today_counters: calls count failed")
            return 0, 0
        phone = facetime = 0
        for row in rows:
            kind = _classify_call(row["service"], self._calls_cfg)
            if kind == "phone":
                phone += int(row["n"])
            elif kind == "facetime":
                facetime += int(row["n"])
        return phone, facetime

    def _count_voicemails(self, since_mat: float) -> int:
        if not self._voicemail_db_path or not self._voicemail_cfg.enabled:
            return 0
        include_deleted_flag = 1 if self._voicemail_cfg.include_deleted else 0
        try:
            with open_ro(self._voicemail_db_path) as conn:
                rows = conn.execute(
                    _VOICEMAILS_COUNT_QUERY, (since_mat, include_deleted_flag)
                ).fetchall()
        except FileNotFoundError:
            return 0
        except Exception:
            logger.exception("today_counters: voicemails count failed")
            return 0
        # Only phone-provider voicemails count toward the "voicemails today"
        # tally — FaceTime audio messages are visually distinct in HA.
        total = 0
        for row in rows:
            kind = _classify_provider(row["provider"], self._voicemail_cfg)
            if kind == "phone":
                total += int(row["n"])
        return total

    # ---- ticker ----

    async def run_once(self, mqtt: MqttPublisher) -> None:
        since_mat = start_of_today_mac_absolute_time(tz=self._tz)
        msgs_received, msgs_sent = self._count_messages(since_mat)
        phone_calls, facetime_calls = self._count_calls(since_mat)
        voicemails = self._count_voicemails(since_mat)

        mqtt.publish_state(self._state_topic("messages_received"), msgs_received)
        mqtt.publish_state(self._state_topic("messages_sent"), msgs_sent)
        mqtt.publish_state(self._state_topic("phone_calls"), phone_calls)
        mqtt.publish_state(self._state_topic("facetime_calls"), facetime_calls)
        mqtt.publish_state(self._state_topic("voicemails"), voicemails)

        logger.info(
            "today_counters tick: messages r=%d s=%d, calls phone=%d facetime=%d, voicemails=%d",
            msgs_received, msgs_sent, phone_calls, facetime_calls, voicemails,
        )

    async def publish_discovery(self, mqtt: MqttPublisher) -> None:
        device = mqtt.host_device_block()

        sensors: list[tuple[str, str, str]] = [
            ("messages_received", "Messages - Received Today", "mdi:message-arrow-left"),
            ("messages_sent", "Messages - Sent Today", "mdi:message-arrow-right"),
            ("phone_calls", "Phone - Calls Today", "mdi:phone"),
            ("facetime_calls", "FaceTime - Calls Today", "mdi:video"),
            ("voicemails", "Voicemail - Today", "mdi:voicemail"),
        ]
        for suffix, name, icon in sensors:
            payload: dict[str, Any] = build_discovery_payload(
                name=name,
                unique_id=self._unique_id(suffix),
                state_topic=self._state_topic(suffix),
                availability_topic=None,
                device=device,
                state_class="total",
                unit_of_measurement="count",
                icon=icon,
            )
            mqtt.publish_discovery(
                component="sensor",
                unique_id=self._unique_id(suffix),
                payload=payload,
            )
