"""Phase D — synced cross-device Screen Time (personal + family).

Reads RMAdminStore-Local.sqlite (which holds iCloud-synced cross-device
data — RMAdminStore-Cloud.sqlite contains only CloudKit metadata).

Devices and users are auto-discovered. One HA sensor pair (today_total +
today_pickups) per discovered (user, device) pair, plus a single "top app
today across all family devices" sensor on a per-household device card.

Phase D entities use their own per-family-member device cards (not the
host's device block) so HA presents them as separate "people" rather than
attaching everyone's stats to the bridge host.

Schema (reverse-engineered):

    ZCOREDEVICE  PK, ZPLATFORM (1=Mac, 2=iPhone/iPad, 4=Watch),
                 ZIDENTIFIER, ZNAME
    ZCOREUSER    PK, ZDSID, ZISFAMILYORGANIZER, ZAPPLEID, ZGIVENNAME
    ZUSAGE       PK, ZDEVICE_FK, ZUSER_FK, ZLASTEVENTDATE
    ZUSAGEBLOCK  PK, ZUSAGE_FK, ZSTARTDATE_MAT,
                 ZSCREENTIMEINSECONDS, ZNUMBEROFPICKUPSWITHOUTAPPLICATIONUSAGE
    ZUSAGECATEGORY PK, ZBLOCK_FK, ZIDENTIFIER, ZTOTALTIMEINSECONDS
    ZUSAGETIMEDITEM PK, ZCATEGORY_FK, ZBUNDLEIDENTIFIER, ZTOTALTIMEINSECONDS

Failure modes (schema-tolerant):
  * sqlite OperationalError (table missing on different macOS versions)
    → log warning, return empty results, keep other phases running.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from macos_bridge.apps import bundle_id_to_app_name
from macos_bridge.config import FamilyMember
from macos_bridge.db import read_only_copy
from macos_bridge.hostname import slugify_hostname
from macos_bridge.mqtt import MqttPublisher, build_discovery_payload
from macos_bridge.phases.base import AbstractTicker
from macos_bridge.time_utils import start_of_today_mac_absolute_time

logger = logging.getLogger(__name__)

_QUERIES_PKG = "macos_bridge.queries"

# ZCOREDEVICE.ZPLATFORM mapping
_PLATFORM_NAMES = {1: "mac", 2: "ios", 4: "watch"}


def _load_query(name: str) -> str:
    return files(_QUERIES_PKG).joinpath(name).read_text()


@dataclass(frozen=True)
class DiscoveredPair:
    """One (user, device) pair found in the synced DB."""

    usage_pk: int
    user_pk: int
    user_dsid: int
    user_name: str
    device_pk: int
    device_name: str
    device_platform: int
    user_slug: str
    device_slug: str

    @property
    def pair_slug(self) -> str:
        return f"{self.user_slug}_{self.device_slug}"


def _slug(name: str) -> str:
    return slugify_hostname(name)


def _build_pair(row: tuple) -> DiscoveredPair:
    usage_pk, user_pk, dsid, user_name, _is_org, device_pk, device_name, platform, _ident = row
    return DiscoveredPair(
        usage_pk=int(usage_pk),
        user_pk=int(user_pk),
        user_dsid=int(dsid) if dsid is not None else 0,
        user_name=user_name or "unknown",
        device_pk=int(device_pk),
        device_name=device_name or "unknown",
        device_platform=int(platform) if platform is not None else 0,
        user_slug=_slug(user_name or "unknown"),
        device_slug=_slug(device_name or "unknown"),
    )


def _build_family_device_block(user_slug: str, user_name: str, sw_version: str) -> dict[str, Any]:
    """One HA device per family member, grouping all that user's per-device sensors."""
    return {
        "identifiers": [f"macos-mqtt-bridge:family:{user_slug}"],
        "name": f"Screen Time — {user_name}",
        "manufacturer": "Apple / Family Sharing",
        "model": "macos-mqtt-bridge",
        "sw_version": sw_version,
    }


class FamilyTicker(AbstractTicker):
    name = "family"

    def __init__(
        self,
        *,
        enabled: bool,
        is_family_organizer: bool,
        interval_seconds: int,
        rm_admin_local_path: Path,
        tmp_dir: Path,
        topic_prefix: str,
        availability_topic: str,
        member_filter: list[FamilyMember] | None = None,
        local_timezone: ZoneInfo | None = None,
    ) -> None:
        self._enabled = enabled
        self._is_family_organizer = is_family_organizer
        self._interval_seconds = interval_seconds
        self._rm_admin_local_path = rm_admin_local_path
        self._tmp_dir = tmp_dir
        self._topic_prefix = topic_prefix
        self._availability = availability_topic
        self._allowed_dsids = (
            {m.dsid for m in member_filter} if member_filter else None
        )
        self._tz = local_timezone
        self._pairs: list[DiscoveredPair] = []

    @property
    def enabled(self) -> bool:
        return self._enabled and self._is_family_organizer

    @property
    def interval_seconds(self) -> int | None:
        return self._interval_seconds

    def _state_topic(self, pair: DiscoveredPair, suffix: str) -> str:
        return (
            f"{self._topic_prefix}/family/{pair.user_slug}/"
            f"{pair.device_slug}/{suffix}"
        )

    def _top_app_topic(self) -> str:
        return f"{self._topic_prefix}/family/top_app/today"

    def _try_query(self, conn: sqlite3.Connection, name: str, params: dict) -> list:
        try:
            return conn.execute(_load_query(name), params).fetchall()
        except sqlite3.OperationalError as exc:
            logger.warning("family %s schema mismatch: %s", name, exc)
            return []

    def _discover(self, conn: sqlite3.Connection) -> list[DiscoveredPair]:
        rows = self._try_query(conn, "family_discover_pairs.sql", {})
        pairs = [_build_pair(r) for r in rows]
        if self._allowed_dsids is not None:
            pairs = [p for p in pairs if p.user_dsid in self._allowed_dsids]
        return pairs

    async def run_once(self, mqtt: MqttPublisher) -> None:
        if not self._rm_admin_local_path.exists():
            logger.warning("family: %s missing; skipping tick", self._rm_admin_local_path)
            return

        since_mat = start_of_today_mac_absolute_time(tz=self._tz)

        with read_only_copy(self._rm_admin_local_path, self._tmp_dir) as conn:
            self._pairs = self._discover(conn)
            per_pair = self._try_query(
                conn, "family_per_pair_today.sql", {"since_mat": since_mat}
            )
            top_apps = self._try_query(
                conn, "family_top_apps_today.sql", {"since_mat": since_mat}
            )

        totals_by_usage: dict[int, tuple[int, int]] = {}
        for row in per_pair:
            usage_pk, _dsid, _device_pk, total_sec, total_pickups, _latest = row
            totals_by_usage[int(usage_pk)] = (int(total_sec or 0), int(total_pickups or 0))

        published = 0
        for pair in self._pairs:
            seconds, pickups = totals_by_usage.get(pair.usage_pk, (0, 0))
            mqtt.publish_state(
                self._state_topic(pair, "today/total"), round(seconds / 3600, 2)
            )
            mqtt.publish_state(self._state_topic(pair, "today/pickups"), pickups)
            published += 1

        if top_apps:
            top_bundle, top_seconds = top_apps[0]
            top_friendly = bundle_id_to_app_name(top_bundle) if top_bundle else "None"
            mqtt.publish_state(self._top_app_topic(), top_friendly)
            mqtt.publish_attributes(
                self._top_app_topic() + "/attrs",
                {
                    "bundle_id": top_bundle or "",
                    "hours": round(float(top_seconds or 0) / 3600, 2),
                    "all": [
                        {
                            "bundle_id": r[0],
                            "friendly_name": bundle_id_to_app_name(r[0]) if r[0] else "",
                            "hours": round(float(r[1] or 0) / 3600, 2),
                        }
                        for r in top_apps
                    ],
                },
            )

        logger.info(
            "family tick: %d (user,device) pairs published; %d top-app rows",
            published, len(top_apps),
        )

    async def publish_discovery(self, mqtt: MqttPublisher) -> None:
        # Re-discover on every discovery refresh so newly-synced devices appear.
        if not self._rm_admin_local_path.exists():
            logger.warning("family: %s missing; skipping discovery", self._rm_admin_local_path)
            return
        with read_only_copy(self._rm_admin_local_path, self._tmp_dir) as conn:
            self._pairs = self._discover(conn)

        for pair in self._pairs:
            device_block = _build_family_device_block(
                pair.user_slug, pair.user_name, mqtt.sw_version
            )
            platform = _PLATFORM_NAMES.get(pair.device_platform, "device")
            icon = {"mac": "mdi:laptop", "ios": "mdi:cellphone", "watch": "mdi:watch"}.get(
                platform, "mdi:devices"
            )

            uid_total = f"macos_family_{pair.pair_slug}_today_total"
            mqtt.publish_discovery(
                component="sensor",
                unique_id=uid_total,
                payload=build_discovery_payload(
                    name=f"{pair.device_name} — Today's Total",
                    unique_id=uid_total,
                    state_topic=self._state_topic(pair, "today/total"),
                    availability_topic=self._availability,
                    device=device_block,
                    device_class="duration",
                    unit_of_measurement="h",
                    state_class="measurement",
                    icon=icon,
                ),
            )
            uid_pickups = f"macos_family_{pair.pair_slug}_today_pickups"
            mqtt.publish_discovery(
                component="sensor",
                unique_id=uid_pickups,
                payload=build_discovery_payload(
                    name=f"{pair.device_name} — Today's Pickups",
                    unique_id=uid_pickups,
                    state_topic=self._state_topic(pair, "today/pickups"),
                    availability_topic=self._availability,
                    device=device_block,
                    state_class="measurement",
                    unit_of_measurement="count",
                    icon="mdi:cellphone-arrow-down",
                ),
            )

        if self._pairs:
            top_device = _build_family_device_block(
                "household", "Household", mqtt.sw_version
            )
            uid_top = "macos_family_top_app_today"
            mqtt.publish_discovery(
                component="sensor",
                unique_id=uid_top,
                payload=build_discovery_payload(
                    name="Top App Today (all devices)",
                    unique_id=uid_top,
                    state_topic=self._top_app_topic(),
                    availability_topic=self._availability,
                    device=top_device,
                    icon="mdi:trophy",
                    json_attributes_topic=self._top_app_topic() + "/attrs",
                ),
            )

        logger.info("family discovery: %d (user,device) pairs", len(self._pairs))
