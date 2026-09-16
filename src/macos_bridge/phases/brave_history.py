"""Phase: Brave Browser per-visit browsing events.

Reads ``~/Library/Application Support/BraveSoftware/Brave-Browser/<Profile>/History``
(Chromium-style SQLite) via the ``read_only_copy`` pattern that already
backs the knowledgeC.db reader. Emits one event per *visit* for today
(Mac-local) — each carrying its real start timestamp and measured dwell
duration — and publishes a single retained JSON blob per profile to
``macos/<host>/today/web/<profile>``.

The Chromium ``visits`` table records ``visit_time`` (per-visit start) and
``visit_duration`` (per-visit dwell) for every page view, so Brave is a
genuine timestamped, duration-bearing activity source — not a daily
aggregate. Telegraf parses the ``visits`` array via ``json_v2``, stamping
each point at its own ``visit_start`` (``timestamp_key``) and writing the
dwell into ``iot_bridges.brave_browsing_seconds`` tagged ``host`` /
``profile`` / ``url`` / ``title`` with field ``duration_s``. The PHP-side
BraveAdapter turns each point into one activity row with real
``started_at`` / ``ended_at``.

Chrome epoch: microseconds since 1601-01-01 UTC; unix offset = 11644473600.
``visit_duration`` is in microseconds — divide by 1e6 to get seconds.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from macos_bridge.db import read_only_copy
from macos_bridge.mqtt import MqttPublisher
from macos_bridge.phases.base import AbstractTicker, screen_time_state_topic

logger = logging.getLogger(__name__)

# Chrome stores timestamps as microseconds since 1601-01-01 UTC.
CHROME_EPOCH_UNIX_OFFSET_SECONDS = 11_644_473_600

# Brave's default per-user Application Support directory.
DEFAULT_BRAVE_HOME = Path("~/Library/Application Support/BraveSoftware/Brave-Browser").expanduser()

# Profile directories follow the Chromium convention: "Default", "Profile 1", ...
PROFILE_GLOB = "Default*"


def _start_of_today_chrome_us(tz: ZoneInfo | None = None) -> int:
    """Return the Chrome-epoch microsecond timestamp for the start of today
    in the given timezone (defaults to system local)."""
    now = datetime.now(tz)
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    unix_seconds = midnight.timestamp()
    return int((unix_seconds + CHROME_EPOCH_UNIX_OFFSET_SECONDS) * 1_000_000)


def _chrome_us_to_iso(visit_time_us: int, tz: ZoneInfo | None = None) -> str:
    """Convert a Chrome-epoch microsecond timestamp to an ISO-8601 string
    with timezone offset (RFC 3339), in ``tz`` (defaults to system local)."""
    unix_seconds = visit_time_us / 1_000_000.0 - CHROME_EPOCH_UNIX_OFFSET_SECONDS
    if tz is not None:
        dt = datetime.fromtimestamp(unix_seconds, tz=tz)
    else:
        # Local time WITH offset (astimezone() on a naive local dt attaches
        # the system zone) so Telegraf can parse it as RFC 3339.
        dt = datetime.fromtimestamp(unix_seconds).astimezone()
    return dt.isoformat()


class BraveHistoryTicker(AbstractTicker):
    name = "brave_history"

    def __init__(
        self,
        *,
        enabled: bool,
        interval_seconds: int,
        brave_home: Path | None = None,
        tmp_dir: Path,
        host_slug: str,
        topic_prefix: str,
        availability_topic: str,
        local_timezone: ZoneInfo | None = None,
        max_visits_per_profile: int = 5000,
    ) -> None:
        self._enabled = enabled
        self._interval_seconds = interval_seconds
        self._brave_home = brave_home or DEFAULT_BRAVE_HOME
        self._tmp_dir = tmp_dir
        self._host_slug = host_slug
        self._topic_prefix = topic_prefix
        self._availability = availability_topic
        self._tz = local_timezone
        self._max_visits_per_profile = max_visits_per_profile

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def interval_seconds(self) -> int | None:
        return self._interval_seconds

    def _state_topic(self, profile: str) -> str:
        return screen_time_state_topic(
            self._topic_prefix, self._host_slug, f"today/web/{profile}"
        )

    def _discover_profiles(self) -> list[Path]:
        """Return Application Support profile dirs that contain a History file."""
        if not self._brave_home.exists():
            logger.warning("Brave home directory not found: %s", self._brave_home)
            return []
        profiles: list[Path] = []
        for entry in sorted(self._brave_home.iterdir()):
            if not entry.is_dir():
                continue
            if not (entry.name == "Default" or entry.name.startswith("Profile ")):
                continue
            if (entry / "History").exists():
                profiles.append(entry)
        return profiles

    def _collect_visits(self, history_path: Path) -> list[dict[str, float | str]]:
        """Open the History snapshot and return today's individual visits.

        Each item: ``{"url", "title", "visit_start" (ISO-8601), "duration_s"}``.
        Ordered newest-first, capped at ``max_visits_per_profile``. Visits with
        zero recorded duration are skipped (Chromium leaves ``visit_duration``
        at 0 for views it couldn't time, e.g. background prerenders).
        """
        since_us = _start_of_today_chrome_us(tz=self._tz)
        try:
            with read_only_copy(history_path, self._tmp_dir) as conn:
                rows = conn.execute(
                    """
                    SELECT u.url AS url,
                           COALESCE(u.title, '') AS title,
                           v.visit_time AS visit_time,
                           v.visit_duration AS visit_duration
                      FROM visits v
                      JOIN urls u ON v.url = u.id
                     WHERE v.visit_time >= ?
                       AND v.visit_duration > 0
                     ORDER BY v.visit_time DESC
                     LIMIT ?
                    """,
                    (since_us, self._max_visits_per_profile),
                ).fetchall()
        except Exception as exc:
            logger.warning("Failed to read Brave History at %s: %s", history_path, exc)
            return []
        visits: list[dict[str, float | str]] = []
        for row in rows:
            url = str(row[0] or "")
            if not url:
                continue
            visits.append(
                {
                    "url": url,
                    "title": str(row[1]),
                    "visit_start": _chrome_us_to_iso(int(row[2]), tz=self._tz),
                    "duration_s": round(float(row[3] or 0) / 1_000_000.0, 3),
                }
            )
        return visits

    async def run_once(self, mqtt: MqttPublisher) -> None:
        profiles = self._discover_profiles()
        if not profiles:
            logger.info("BraveHistoryTicker: no profiles found")
            return

        today_iso = datetime.now(self._tz).strftime("%Y-%m-%d")
        for profile_dir in profiles:
            profile_name = profile_dir.name
            history_path = profile_dir / "History"
            visits = self._collect_visits(history_path)
            payload = {
                "date": today_iso,
                "profile": profile_name,
                "visit_count": len(visits),
                "visits": visits,
            }
            topic = self._state_topic(profile_name.replace(" ", "_"))
            mqtt.publish_state(topic, json.dumps(payload, ensure_ascii=False))
            logger.info(
                "BraveHistoryTicker: published %d visits for profile '%s'",
                len(visits),
                profile_name,
            )
