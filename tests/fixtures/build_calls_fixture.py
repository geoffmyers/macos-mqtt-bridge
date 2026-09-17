# tests/fixtures/build_calls_fixture.py
"""Build a synthetic CallHistory.storedata for testing the Calls source.

Schema is the subset ``sources/calls.py`` queries from ZCALLRECORD:
Z_PK, ZDATE, ZDURATION, ZADDRESS, ZSERVICE_PROVIDER, ZORIGINATED,
ZANSWERED, ZCALLTYPE, ZNAME, ZUNIQUE_ID, ZISO_COUNTRY_CODE,
ZWASEMERGENCYCALL, ZHASMESSAGE, ZCONVERSATIONID.

``ZADDRESS`` rotates across the same fictional-NANP / example.com handles
as ``build_messages_fixture.py`` (two resolve against the synthetic
AddressBook fixture, two do not), and ``ZSERVICE_PROVIDER`` uses Apple's
real provider bundle IDs (``com.apple.Telephony`` / ``com.apple.FaceTime``)
so the default ``phone_service_substrings`` / ``facetime_service_substrings``
classification is exercised the same way it would be against a real DB.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

from macos_bridge.time_utils import datetime_to_mac_absolute_time

ANCHOR_TODAY = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)

ADDRESSES = [
    "+16125550123",  # matches Alex Example's phone
    "jordan.example@example.com",  # matches Jordan Example's email
    "+16125550199",  # also matches Jordan Example (via phone)
    "+16135550101",  # no AddressBook match
]

PHONE_PROVIDER = "com.apple.Telephony"
FACETIME_PROVIDER = "com.apple.FaceTime"


def _mac_seconds(dt: datetime) -> float:
    """CallHistory.storedata ZDATE is seconds (CoreData NSDate) since 2001-01-01."""
    return datetime_to_mac_absolute_time(dt)


def build_calls_fixture(path: Path) -> None:
    if path.exists():
        path.unlink()
    conn = sqlite3.connect(path)
    conn.execute(
        """
        CREATE TABLE ZCALLRECORD (
            Z_PK INTEGER PRIMARY KEY,
            ZDATE FLOAT,
            ZDURATION FLOAT,
            ZADDRESS TEXT,
            ZSERVICE_PROVIDER TEXT,
            ZORIGINATED INTEGER,
            ZANSWERED INTEGER,
            ZCALLTYPE INTEGER,
            ZNAME TEXT,
            ZUNIQUE_ID TEXT,
            ZISO_COUNTRY_CODE TEXT,
            ZWASEMERGENCYCALL INTEGER,
            ZHASMESSAGE INTEGER,
            ZCONVERSATIONID TEXT
        )
        """
    )

    rows = []
    pk = 1
    # A steady rotation of every (provider, originated, answered) combo so
    # every source._classify / direction / answered branch has coverage,
    # plus enough volume for the "last N" window tests.
    combos = [
        (PHONE_PROVIDER, 0, 1),  # incoming answered (phone)
        (PHONE_PROVIDER, 0, 0),  # incoming missed (phone)
        (PHONE_PROVIDER, 1, 1),  # outgoing answered (phone)
        (FACETIME_PROVIDER, 0, 1),  # incoming answered (facetime)
        (FACETIME_PROVIDER, 0, 0),  # incoming missed (facetime)
        (FACETIME_PROVIDER, 1, 1),  # outgoing answered (facetime)
    ]
    for i in range(40):
        provider, originated, answered = combos[i % len(combos)]
        address = ADDRESSES[i % len(ADDRESSES)]
        dt = ANCHOR_TODAY - timedelta(minutes=(40 - i))
        rows.append(
            (
                pk,
                _mac_seconds(dt),
                float(30 + i) if answered else 0.0,
                address,
                provider,
                originated,
                answered,
                1,
                None,
                f"FIXTURE-CALL-{pk:04d}",
                "us",
                0,
                0,
                None,
            )
        )
        pk += 1

    conn.executemany(
        "INSERT INTO ZCALLRECORD ("
        "Z_PK, ZDATE, ZDURATION, ZADDRESS, ZSERVICE_PROVIDER, ZORIGINATED, "
        "ZANSWERED, ZCALLTYPE, ZNAME, ZUNIQUE_ID, ZISO_COUNTRY_CODE, "
        "ZWASEMERGENCYCALL, ZHASMESSAGE, ZCONVERSATIONID"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        rows,
    )
    conn.commit()
    conn.close()
