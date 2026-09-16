"""Phase A: daily headline aggregates."""

from __future__ import annotations

import logging
from importlib.resources import files
from pathlib import Path
from zoneinfo import ZoneInfo

from macos_bridge.apps import bundle_id_to_app_name
from macos_bridge.db import read_only_copy
from macos_bridge.mqtt import MqttPublisher, build_discovery_payload
from macos_bridge.phases.base import (
    AbstractTicker,
    screen_time_state_topic,
    screen_time_unique_id,
)
from macos_bridge.time_utils import start_of_today_mac_absolute_time

logger = logging.getLogger(__name__)

_QUERIES_PKG = "macos_bridge.queries"


def _load_query(name: str) -> str:
    return files(_QUERIES_PKG).joinpath(name).read_text()


class AggregatesTicker(AbstractTicker):
    name = "aggregates"

    def __init__(
        self,
        *,
        enabled: bool,
        interval_seconds: int,
        knowledge_db_path: Path,
        tmp_dir: Path,
        host_slug: str,
        topic_prefix: str,
        availability_topic: str,
        local_timezone: ZoneInfo | None = None,
    ) -> None:
        self._enabled = enabled
        self._interval_seconds = interval_seconds
        self._knowledge_db_path = knowledge_db_path
        self._tmp_dir = tmp_dir
        self._host_slug = host_slug
        self._topic_prefix = topic_prefix
        self._availability = availability_topic
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

    async def run_once(self, mqtt: MqttPublisher) -> None:
        since_mat = start_of_today_mac_absolute_time(tz=self._tz)
        with read_only_copy(self._knowledge_db_path, self._tmp_dir) as conn:
            total_row = conn.execute(
                _load_query("daily_total.sql"), {"since_mat": since_mat}
            ).fetchone()
            pickups_row = conn.execute(
                _load_query("pickups.sql"), {"since_mat": since_mat}
            ).fetchone()
            top_row = conn.execute(
                _load_query("top_app.sql"), {"since_mat": since_mat}
            ).fetchone()

        total_hours = round((total_row[0] or 0.0) / 3600, 2)
        pickups = int(pickups_row[0] or 0)
        top_bundle, top_seconds = (top_row[0], top_row[1]) if top_row else (None, 0.0)
        top_hours = round((top_seconds or 0.0) / 3600, 2)
        top_friendly = bundle_id_to_app_name(top_bundle) if top_bundle else "None"

        mqtt.publish_state(self._state_topic("total"), total_hours)
        mqtt.publish_state(self._state_topic("pickups"), pickups)
        mqtt.publish_state(self._state_topic("top_app"), top_friendly)
        mqtt.publish_attributes(
            self._state_topic("top_app") + "/attrs",
            {"bundle_id": top_bundle or ""},
        )
        mqtt.publish_state(self._state_topic("top_app_minutes"), top_hours)

        logger.info(
            "aggregates tick: total=%.2fh pickups=%d top=%s [%s] (%.2fh)",
            total_hours, pickups, top_friendly, top_bundle, top_hours,
        )

    async def publish_discovery(self, mqtt: MqttPublisher) -> None:
        device = mqtt.host_device_block()
        for suffix, name, device_class, unit, icon in [
            ("total", "Today's Total", "duration", "h", "mdi:laptop"),
            ("pickups", "Today's Pickups", None, "count", "mdi:cellphone-arrow-down"),
            ("top_app", "Today's Top App", None, None, "mdi:star"),
            ("top_app_minutes", "Today's Top App Hours", "duration", "h", "mdi:star-outline"),
        ]:
            json_attrs_topic = (
                self._state_topic(suffix) + "/attrs" if suffix == "top_app" else None
            )
            mqtt.publish_discovery(
                component="sensor",
                unique_id=self._unique_id(suffix),
                payload=build_discovery_payload(
                    name=name,
                    unique_id=self._unique_id(suffix),
                    state_topic=self._state_topic(suffix),
                    availability_topic=None,
                    device=device,
                    device_class=device_class,
                    unit_of_measurement=unit,
                    state_class="measurement" if unit else None,
                    icon=icon,
                    json_attributes_topic=json_attrs_topic,
                ),
            )
