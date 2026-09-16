"""Phase B: per-app daily usage from a curated allow-list."""

from __future__ import annotations

import logging
from importlib.resources import files
from pathlib import Path
from zoneinfo import ZoneInfo

from macos_bridge.apps import bundle_id_to_app_name
from macos_bridge.config import AppEntry
from macos_bridge.db import read_only_copy
from macos_bridge.hostname import slugify_hostname
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


def _bundle_slug(bundle_id: str) -> str:
    """Slugify a bundle id into an HA-unique-id-safe token. Reuses the hostname
    slugifier (lowercase, replace non-alnum with `_`, collapse, strip).
    e.g. ``com.microsoft.VSCode`` → ``com_microsoft_vscode``."""
    return slugify_hostname(bundle_id)


class PerAppTicker(AbstractTicker):
    name = "per_app"

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
        apps: list[AppEntry],
        local_timezone: ZoneInfo | None = None,
    ) -> None:
        self._enabled = enabled
        self._interval_seconds = interval_seconds
        self._knowledge_db_path = knowledge_db_path
        self._tmp_dir = tmp_dir
        self._host_slug = host_slug
        self._topic_prefix = topic_prefix
        self._availability = availability_topic
        self._apps = apps
        self._tz = local_timezone

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def interval_seconds(self) -> int | None:
        return self._interval_seconds

    def _state_topic(self, bundle_id: str) -> str:
        return screen_time_state_topic(
            self._topic_prefix, self._host_slug, f"today/apps/{bundle_id}"
        )

    def _unique_id(self, bundle_id: str) -> str:
        return screen_time_unique_id(
            self._host_slug, "today", "app", _bundle_slug(bundle_id)
        )

    def _friendly_name(self, app: AppEntry) -> str:
        if app.friendly_name:
            return app.friendly_name
        return bundle_id_to_app_name(app.bundle_id)

    async def run_once(self, mqtt: MqttPublisher) -> None:
        if not self._apps:
            return
        since_mat = start_of_today_mac_absolute_time(tz=self._tz)
        with read_only_copy(self._knowledge_db_path, self._tmp_dir) as conn:
            rows = conn.execute(
                _load_query("per_app_today.sql"),
                {"since_mat": since_mat},
            ).fetchall()

        seconds_by_bundle: dict[str, float] = {row[0]: float(row[1] or 0.0) for row in rows}

        for app in self._apps:
            seconds = seconds_by_bundle.get(app.bundle_id, 0.0)
            hours = round(seconds / 3600, 2)
            mqtt.publish_state(self._state_topic(app.bundle_id), hours)

        logger.info(
            "per_app tick: %d allow-listed apps, total %.2fh",
            len(self._apps),
            sum(seconds_by_bundle.get(a.bundle_id, 0.0) for a in self._apps) / 3600,
        )

    async def publish_discovery(self, mqtt: MqttPublisher) -> None:
        device = mqtt.host_device_block()
        for app in self._apps:
            display = self._friendly_name(app)
            mqtt.publish_discovery(
                component="sensor",
                unique_id=self._unique_id(app.bundle_id),
                payload=build_discovery_payload(
                    name=f"App Time Today - {display}",
                    unique_id=self._unique_id(app.bundle_id),
                    state_topic=self._state_topic(app.bundle_id),
                    availability_topic=None,
                    device=device,
                    device_class="duration",
                    unit_of_measurement="h",
                    state_class="measurement",
                    icon=app.icon or "mdi:application",
                ),
            )
