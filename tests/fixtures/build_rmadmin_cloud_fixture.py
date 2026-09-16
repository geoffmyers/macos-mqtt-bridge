"""Build a synthetic RMAdminStore-Local.sqlite for Phase D testing.

Schema matches the reverse-engineered subset Phase D queries actually use:

    ZCOREDEVICE  PK, ZPLATFORM, ZIDENTIFIER, ZNAME
    ZCOREUSER    PK, ZDSID, ZISFAMILYORGANIZER, ZAPPLEID, ZGIVENNAME
    ZUSAGE       PK, ZDEVICE, ZUSER, ZLASTEVENTDATE
    ZUSAGEBLOCK  PK, ZUSAGE, ZSTARTDATE,
                 ZSCREENTIMEINSECONDS, ZNUMBEROFPICKUPSWITHOUTAPPLICATIONUSAGE
    ZUSAGECATEGORY PK, ZBLOCK, ZIDENTIFIER, ZTOTALTIMEINSECONDS
    ZUSAGETIMEDITEM PK, ZCATEGORY, ZBUNDLEIDENTIFIER, ZTOTALTIMEINSECONDS

(File name retained for backward compat with the conftest fixture name
even though the real path is now RMAdminStore-Local.sqlite.)
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from macos_bridge.time_utils import datetime_to_mac_absolute_time
from tests.fixtures.build_knowledge_fixture import ANCHOR_TODAY


def build_rmadmin_cloud_fixture(path: Path) -> None:
    """Build a synthetic RMAdminStore-Local.sqlite at `path`.

    Layout:
      - 2 users: Alex (organizer, dsid=111), Jordan (dsid=222)
      - 4 devices: Alex's MacBook Pro (mac), Alex's iPhone (ios),
                   Alex's Apple Watch (watch), Jordan's iPad (ios)
      - 4 ZUSAGE rows (one per user-device pair):
          (Alex, Mac)    today_total = 90 min, pickups = 5
          (Alex, iPhone) today_total = 45 min, pickups = 12
          (Alex, Watch)  today_total = 5 min,  pickups = 2
          (Jordan, iPad)    today_total = 120 min, pickups = 20
      - Top app today (across all devices): Safari, 75 min
    """
    if path.exists():
        path.unlink()
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE ZCOREDEVICE (
            Z_PK INTEGER PRIMARY KEY,
            ZPLATFORM INTEGER,
            ZIDENTIFIER VARCHAR,
            ZNAME VARCHAR
        );
        CREATE TABLE ZCOREUSER (
            Z_PK INTEGER PRIMARY KEY,
            ZDSID INTEGER,
            ZISFAMILYORGANIZER INTEGER,
            ZAPPLEID VARCHAR,
            ZGIVENNAME VARCHAR
        );
        CREATE TABLE ZUSAGE (
            Z_PK INTEGER PRIMARY KEY,
            ZDEVICE INTEGER,
            ZUSER INTEGER,
            ZLASTEVENTDATE FLOAT
        );
        CREATE TABLE ZUSAGEBLOCK (
            Z_PK INTEGER PRIMARY KEY,
            ZUSAGE INTEGER,
            ZSTARTDATE FLOAT,
            ZSCREENTIMEINSECONDS INTEGER,
            ZNUMBEROFPICKUPSWITHOUTAPPLICATIONUSAGE INTEGER
        );
        CREATE TABLE ZUSAGECATEGORY (
            Z_PK INTEGER PRIMARY KEY,
            ZBLOCK INTEGER,
            ZIDENTIFIER VARCHAR,
            ZTOTALTIMEINSECONDS INTEGER
        );
        CREATE TABLE ZUSAGETIMEDITEM (
            Z_PK INTEGER PRIMARY KEY,
            ZCATEGORY INTEGER,
            ZBUNDLEIDENTIFIER VARCHAR,
            ZTOTALTIMEINSECONDS INTEGER
        );
        """
    )

    today_start = ANCHOR_TODAY.replace(hour=0, minute=0, second=0, microsecond=0)
    today_start_mat = datetime_to_mac_absolute_time(today_start)

    conn.executemany(
        "INSERT INTO ZCOREDEVICE (Z_PK, ZPLATFORM, ZIDENTIFIER, ZNAME) VALUES (?, ?, ?, ?)",
        [
            (1, 1, "MAC-UUID", "Alex's MacBook Pro"),
            (2, 2, "IPHONE-UUID", "Alex's iPhone"),
            (3, 4, "WATCH-UUID", "Alex's Apple Watch"),
            (4, 2, "IPAD-UUID", "Jordan's iPad"),
        ],
    )
    conn.executemany(
        "INSERT INTO ZCOREUSER (Z_PK, ZDSID, ZISFAMILYORGANIZER, ZAPPLEID, ZGIVENNAME) "
        "VALUES (?, ?, ?, ?, ?)",
        [
            (1, 111, 1, "alex@example.com", "Alex"),
            (2, 222, 0, "jordan@example.com", "Jordan"),
        ],
    )

    usage_rows = [
        (10, 1, 1, today_start_mat),  # Alex, Mac
        (11, 2, 1, today_start_mat),  # Alex, iPhone
        (12, 3, 1, today_start_mat),  # Alex, Watch
        (13, 4, 2, today_start_mat),  # Jordan, iPad
    ]
    conn.executemany(
        "INSERT INTO ZUSAGE (Z_PK, ZDEVICE, ZUSER, ZLASTEVENTDATE) VALUES (?, ?, ?, ?)",
        usage_rows,
    )

    block_rows = [
        (100, 10, today_start_mat + 9 * 3600, 90 * 60, 5),
        (101, 11, today_start_mat + 9 * 3600, 45 * 60, 12),
        (102, 12, today_start_mat + 9 * 3600, 5 * 60, 2),
        (103, 13, today_start_mat + 9 * 3600, 120 * 60, 20),
    ]
    conn.executemany(
        "INSERT INTO ZUSAGEBLOCK "
        "(Z_PK, ZUSAGE, ZSTARTDATE, ZSCREENTIMEINSECONDS, "
        "ZNUMBEROFPICKUPSWITHOUTAPPLICATIONUSAGE) VALUES (?, ?, ?, ?, ?)",
        block_rows,
    )

    conn.execute(
        "INSERT INTO ZUSAGECATEGORY (Z_PK, ZBLOCK, ZIDENTIFIER, ZTOTALTIMEINSECONDS) "
        "VALUES (?, ?, ?, ?)",
        (200, 100, "productivity", 75 * 60),
    )
    conn.executemany(
        "INSERT INTO ZUSAGETIMEDITEM "
        "(Z_PK, ZCATEGORY, ZBUNDLEIDENTIFIER, ZTOTALTIMEINSECONDS) VALUES (?, ?, ?, ?)",
        [
            (300, 200, "com.apple.Safari", 75 * 60),
            (301, 200, "com.apple.logic10", 30 * 60),
        ],
    )

    conn.commit()
    conn.close()
