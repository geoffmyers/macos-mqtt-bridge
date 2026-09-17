# tests/fixtures/build_messages_fixture.py
"""Build a synthetic chat.db for testing the Messages source.

Schema is the subset ``sources/messages.py`` queries:

    message              ROWID, guid, text, attributedBody, is_from_me, date,
                          date_edited, date_retracted, service,
                          associated_message_type, associated_message_guid,
                          associated_message_emoji, item_type,
                          group_action_type, group_title, other_handle,
                          cache_has_attachments, handle_id
    handle                ROWID, id
    chat                  ROWID, guid, style, display_name
    chat_message_join     chat_id, message_id

Every name, number and message body is invented: handles use the same
NANP fictional range (555-0100 to 555-0199) and example.com addresses as
``build_address_book_fixture.py``, and two of them are chosen so they
resolve against that fixture's Alex/Jordan contacts (proving the
end-to-end contact-enrichment path with synthetic data only).

Layout (ROWID ranges, all deterministic so tests can target them):

  1-200    "history" messages exercising the text / attributedBody-fallback
           decode paths (see _TextMode below), rotated across four handles
           (two of which match the synthetic AddressBook) and two chats
           (one 1:1, one group).
  201      a reaction (tapback) on an earlier message
  202      a group-membership/name-change event
  203      an edited message
  204      a retracted message
  205-220  plain sent/received messages — guarantees the newest few rows
           (what "poll since last-seen" tests read) are always ordinary
           messages, never a reaction/group/edited/retracted row.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from enum import Enum
from pathlib import Path

from macos_bridge.time_utils import datetime_to_mac_absolute_time

# Fixed anchor so timestamps are deterministic across runs.
ANCHOR_TODAY = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)

# Handles: two resolve against tests/fixtures/build_address_book_fixture.py's
# synthetic contacts (Alex via phone, Jordan via email), two do not.
HANDLE_ALEX_PHONE = "+16125550123"  # matches Alex Example's phone
HANDLE_JORDAN_EMAIL = "jordan.example@example.com"  # matches Jordan Example's email
HANDLE_JORDAN_PHONE = "+16125550199"  # also matches Jordan Example (via phone)
HANDLE_UNKNOWN = "+16135550101"  # no AddressBook match

HANDLES = [HANDLE_ALEX_PHONE, HANDLE_JORDAN_EMAIL, HANDLE_JORDAN_PHONE, HANDLE_UNKNOWN]

SERVICES = ["iMessage", "SMS", "RCS"]


class _TextMode(Enum):
    TEXT_ONLY = 0
    ATTRIBUTED_ONLY = 1
    BOTH = 2


def _mac_ns(dt: datetime) -> int:
    """chat.db `message.date` is nanoseconds since 2001-01-01 (post-El Capitan)."""
    return int(datetime_to_mac_absolute_time(dt) * 1_000_000_000)


def _attributed_body(text: str) -> bytes:
    """Minimal NSKeyedArchiver-typedstream-shaped blob that
    ``_extract_attributed_text`` (a hand-rolled scanner, not a full
    NSKeyedUnarchiver) can decode: the ``NSString`` marker, a run of
    marker bytes it skips, then a single length-prefixed short string."""
    text_bytes = text.encode("utf-8")
    if len(text_bytes) >= 128:
        text_bytes = text_bytes[:127]
    return b"NSString" + bytes([0x01, 0x84, 0x94, 0x2B, len(text_bytes)]) + text_bytes


def build_messages_fixture(path: Path) -> None:
    if path.exists():
        path.unlink()
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE message (
            ROWID INTEGER PRIMARY KEY,
            guid TEXT,
            text TEXT,
            attributedBody BLOB,
            is_from_me INTEGER,
            date INTEGER,
            date_edited INTEGER,
            date_retracted INTEGER,
            service TEXT,
            associated_message_type INTEGER,
            associated_message_guid TEXT,
            associated_message_emoji TEXT,
            item_type INTEGER,
            group_action_type INTEGER,
            group_title TEXT,
            other_handle INTEGER,
            cache_has_attachments INTEGER,
            handle_id INTEGER
        );
        CREATE TABLE handle (ROWID INTEGER PRIMARY KEY, id TEXT);
        CREATE TABLE chat (
            ROWID INTEGER PRIMARY KEY,
            guid TEXT,
            style INTEGER,
            display_name TEXT
        );
        CREATE TABLE chat_message_join (chat_id INTEGER, message_id INTEGER);
        """
    )

    # Handles: ROWID 1..4, in HANDLES order.
    for i, h in enumerate(HANDLES, start=1):
        conn.execute("INSERT INTO handle (ROWID, id) VALUES (?, ?)", (i, h))

    # Chats: 1 = 1:1, 2 = group (style 43).
    conn.execute(
        "INSERT INTO chat (ROWID, guid, style, display_name) VALUES (1, ?, 45, NULL)",
        ("iMessage;-;fixture-1on1",),
    )
    conn.execute(
        "INSERT INTO chat (ROWID, guid, style, display_name) VALUES (2, ?, 43, ?)",
        ("iMessage;-;fixture-group", "Fixture Group"),
    )

    def insert_message(rowid: int, dt: datetime, handle_rowid: int, chat_rowid: int, **cols) -> None:
        row = {
            "ROWID": rowid,
            "guid": f"FIXTURE-MSG-{rowid:04d}",
            "text": None,
            "attributedBody": None,
            "is_from_me": 0,
            "date": _mac_ns(dt),
            "date_edited": 0,
            "date_retracted": 0,
            "service": SERVICES[rowid % len(SERVICES)],
            "associated_message_type": 0,
            "associated_message_guid": None,
            "associated_message_emoji": None,
            "item_type": 0,
            "group_action_type": None,
            "group_title": None,
            "other_handle": None,
            "cache_has_attachments": 0,
        }
        row.update(cols)
        conn.execute(
            "INSERT INTO message ("
            "ROWID, guid, text, attributedBody, is_from_me, date, date_edited, "
            "date_retracted, service, associated_message_type, "
            "associated_message_guid, associated_message_emoji, item_type, "
            "group_action_type, group_title, other_handle, cache_has_attachments, "
            "handle_id"
            ") VALUES ("
            ":ROWID, :guid, :text, :attributedBody, :is_from_me, :date, "
            ":date_edited, :date_retracted, :service, :associated_message_type, "
            ":associated_message_guid, :associated_message_emoji, :item_type, "
            ":group_action_type, :group_title, :other_handle, :cache_has_attachments, "
            ":handle_id"
            ")",
            {**row, "handle_id": handle_rowid},
        )
        conn.execute(
            "INSERT INTO chat_message_join (chat_id, message_id) VALUES (?, ?)",
            (chat_rowid, rowid),
        )

    # 1-200: history messages exercising all three text/attributedBody modes.
    for r in range(1, 201):
        dt = ANCHOR_TODAY - timedelta(minutes=(201 - r))
        handle_rowid = 1 + (r % len(HANDLES))
        chat_rowid = 2 if r % 5 == 0 else 1
        body = f"Fixture message {r} about the weekend plan"
        mode = _TextMode(r % 3)
        if mode is _TextMode.TEXT_ONLY:
            text, blob = body, None
        elif mode is _TextMode.ATTRIBUTED_ONLY:
            text, blob = None, _attributed_body(body)
        else:
            text, blob = body, _attributed_body(body)
        insert_message(
            r, dt, handle_rowid, chat_rowid,
            text=text, attributedBody=blob, is_from_me=r % 2,
        )

    base = ANCHOR_TODAY

    # 201: a reaction (Loved) on message 100.
    insert_message(
        201, base + timedelta(seconds=1), 1, 1,
        is_from_me=1, associated_message_type=2000,
        associated_message_guid="p:0/FIXTURE-MSG-0100",
        associated_message_emoji=None,
    )

    # 202: a group participant/name-change event.
    insert_message(
        202, base + timedelta(seconds=2), 2, 2,
        is_from_me=0, item_type=1, group_action_type=1,
        group_title="Fixture Group (renamed)", other_handle=3,
    )

    # 203: an edited message.
    insert_message(
        203, base + timedelta(seconds=3), 1, 1,
        is_from_me=1, text="Fixture edited message body",
        date_edited=_mac_ns(base + timedelta(seconds=30)),
    )

    # 204: a retracted (unsent) message.
    insert_message(
        204, base + timedelta(seconds=4), 2, 1,
        is_from_me=0, text="Fixture retracted message body",
        date_retracted=_mac_ns(base + timedelta(seconds=40)),
    )

    # 205-220: plain sent/received tail — guarantees "just the newest rows"
    # tests always see ordinary messages.
    for i, r in enumerate(range(205, 221)):
        handle_rowid = 1 + (r % len(HANDLES))
        insert_message(
            r, base + timedelta(seconds=10 + i), handle_rowid, 1,
            is_from_me=r % 2,
            text=f"Fixture tail message {r}",
            attributedBody=_attributed_body(f"Fixture tail message {r}"),
        )

    conn.commit()
    conn.close()
