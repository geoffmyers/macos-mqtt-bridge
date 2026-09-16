# tests/fixtures/build_knowledge_fixture.py
"""Build a synthetic knowledgeC.db for testing.

Schema is the minimum subset Phase A queries need:
ZOBJECT(ZSTREAMNAME TEXT, ZSTARTDATE FLOAT, ZENDDATE FLOAT,
ZVALUESTRING TEXT, ZVALUEINTEGER INTEGER).

Real knowledgeC.db has many more columns; queries reference only these.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from macos_bridge.time_utils import datetime_to_mac_absolute_time

# Fixed "today" anchor for deterministic tests
ANCHOR_TODAY = datetime(2026, 4, 26, 12, 0, 0, tzinfo=UTC)


def build_fixture(path: Path) -> None:
    """Build a knowledgeC.db at `path` with a deterministic set of events.

    Today's data:
      - Safari: 30 minutes (one session 09:00 - 09:30 local-equivalent)
      - Slack: 15 minutes
      - Logic Pro: 45 minutes  ← top app
      - 7 backlight-on events  ← pickups
    Yesterday's data (must be excluded by 'since today' filter):
      - Safari: 60 minutes (should NOT appear in today's totals)
    """
    if path.exists():
        path.unlink()
    conn = sqlite3.connect(path)
    conn.execute(
        """
        CREATE TABLE ZOBJECT (
            Z_PK INTEGER PRIMARY KEY,
            ZSTREAMNAME TEXT,
            ZSTARTDATE FLOAT,
            ZENDDATE FLOAT,
            ZVALUESTRING TEXT,
            ZVALUEINTEGER INTEGER
        )
        """
    )

    today_start = ANCHOR_TODAY.replace(hour=0, minute=0, second=0, microsecond=0)
    today_start_mat = datetime_to_mac_absolute_time(today_start)
    yesterday_start_mat = today_start_mat - 86400

    rows = []
    # Today: Safari 30 min, Slack 15 min, Logic Pro 45 min
    rows.append(
        (
            "/app/usage",
            today_start_mat + 9 * 3600,
            today_start_mat + 9 * 3600 + 1800,
            "com.apple.Safari",
            None,
        )
    )
    rows.append(
        (
            "/app/usage",
            today_start_mat + 10 * 3600,
            today_start_mat + 10 * 3600 + 900,
            "com.tinyspeck.slackmacgap",
            None,
        )
    )
    rows.append(
        (
            "/app/usage",
            today_start_mat + 11 * 3600,
            today_start_mat + 11 * 3600 + 2700,
            "com.apple.logic10",
            None,
        )
    )
    # Daemons / helpers that should be filtered out by bootstrap-allowlist
    rows.append(
        (
            "/app/usage",
            today_start_mat + 8 * 3600,
            today_start_mat + 8 * 3600 + 600,
            "com.apple.WindowManager",
            None,
        )
    )
    rows.append(
        (
            "/app/usage",
            today_start_mat + 8 * 3600 + 600,
            today_start_mat + 8 * 3600 + 1200,
            "com.apple.someservice.helper",
            None,
        )
    )
    # 7 pickups today
    for i in range(7):
        ts = today_start_mat + (8 + i) * 3600
        rows.append(("/display/isBacklit", ts, ts + 1, None, 1))
    # /app/inFocus events for Phase C — three transitions today
    rows.append(
        (
            "/app/inFocus",
            today_start_mat + 9 * 3600,
            today_start_mat + 9 * 3600 + 1800,
            "com.apple.Safari",
            None,
        )
    )
    rows.append(
        (
            "/app/inFocus",
            today_start_mat + 10 * 3600,
            today_start_mat + 10 * 3600 + 900,
            "com.tinyspeck.slackmacgap",
            None,
        )
    )
    rows.append(
        (
            "/app/inFocus",
            today_start_mat + 11 * 3600,
            today_start_mat + 11 * 3600 + 2700,
            "com.apple.logic10",
            None,
        )
    )
    # /device/isLocked events — locked then unlocked
    rows.append(
        ("/device/isLocked", today_start_mat + 7 * 3600, today_start_mat + 7 * 3600 + 1, None, 0)
    )
    # Yesterday's Safari (must be excluded by today-filtered Phase A/B queries)
    rows.append(
        (
            "/app/usage",
            yesterday_start_mat + 9 * 3600,
            yesterday_start_mat + 9 * 3600 + 3600,
            "com.apple.Safari",
            None,
        )
    )

    conn.executemany(
        "INSERT INTO ZOBJECT (ZSTREAMNAME, ZSTARTDATE, ZENDDATE, ZVALUESTRING, ZVALUEINTEGER) "
        "VALUES (?, ?, ?, ?, ?)",
        rows,
    )
    conn.commit()
    conn.close()
