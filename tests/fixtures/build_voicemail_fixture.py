# tests/fixtures/build_voicemail_fixture.py
"""Build a synthetic FaceTimeMessageStore-local.sqlitedb for testing the
Voicemail source (which also carries FaceTime audio-message transcripts —
same table, see ``sources/voicemail.py``).

Schema is the subset queried from ZSTOREDMESSAGE: Z_PK, ZMESSAGETYPE,
ZMAILBOXTYPE, ZTRANSCRIPTIONSTATUS, ZDATECREATED, ZDATEMODIFIED,
ZDURATION, ZFROM, ZRECIPIENT, ZPROVIDER, ZSIMID, ZRECORDUUID, ZCALLUUID,
ZTRANSCRIPT, ZISREAD.

``ZTRANSCRIPT`` is a real NSKeyedArchiver binary plist (built with
``plistlib`` + ``plistlib.UID``, not a hand-rolled approximation) shaped
like Apple's "Shape A" — ``root = {transcriptionString: UID}`` — which is
what ``_decode_transcript`` in ``sources/voicemail.py`` unarchives.
"""

from __future__ import annotations

import plistlib
import sqlite3
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

from macos_bridge.time_utils import datetime_to_mac_absolute_time

ANCHOR_TODAY = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)

FROM_ADDRESSES = [
    "+16125550123",  # matches Alex Example's phone
    "jordan.example@example.com",  # matches Jordan Example's email
    "+16135550101",  # no AddressBook match
]

PHONE_PROVIDER = "com.apple.coretelephony"
FACETIME_PROVIDER = "com.apple.facetime"

# Z_PK values (1-indexed) that are "deleted" (ZMAILBOXTYPE=2), exercising
# the include_deleted default-false filter.
DELETED_PKS = {10, 20}


def _mac_seconds(dt: datetime) -> float:
    """FaceTimeMessageStore-local.sqlitedb ZDATECREATED is seconds since 2001-01-01."""
    return datetime_to_mac_absolute_time(dt)


def _record_uuid_bytes(pk: int) -> bytes:
    """Deterministic 16-byte UUID for a given Z_PK, shared by both the DB
    build (ZRECORDUUID) and the matching synthetic Assets/ tree, so the two
    line up the same way a real ZRECORDUUID and its Assets file do."""
    return uuid.uuid5(uuid.NAMESPACE_URL, f"fixture-voicemail-{pk}").bytes


def _record_uuid_str(pk: int) -> str:
    """Same formatting as sources/voicemail.py's _format_uuid()."""
    h = _record_uuid_bytes(pk).hex().upper()
    return f"{h[0:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:32]}"


def _transcript_blob(text: str) -> bytes:
    """A real NSKeyedArchiver binary plist, Shape A:
    root = {transcriptionString: UID(2)}; objects[2] = the text."""
    objects = ["$null", {"transcriptionString": plistlib.UID(2)}, text]
    top = {"root": plistlib.UID(1)}
    plist = {
        "$archiver": "NSKeyedArchiver",
        "$objects": objects,
        "$top": top,
        "$version": 100000,
    }
    return plistlib.dumps(plist, fmt=plistlib.FMT_BINARY)


def build_voicemail_fixture(path: Path) -> None:
    if path.exists():
        path.unlink()
    conn = sqlite3.connect(path)
    conn.execute(
        """
        CREATE TABLE ZSTOREDMESSAGE (
            Z_PK INTEGER PRIMARY KEY,
            ZMESSAGETYPE INTEGER,
            ZMAILBOXTYPE INTEGER,
            ZTRANSCRIPTIONSTATUS INTEGER,
            ZDATECREATED FLOAT,
            ZDATEMODIFIED FLOAT,
            ZDURATION FLOAT,
            ZFROM TEXT,
            ZRECIPIENT TEXT,
            ZPROVIDER TEXT,
            ZSIMID TEXT,
            ZRECORDUUID BLOB,
            ZCALLUUID TEXT,
            ZTRANSCRIPT BLOB,
            ZISREAD INTEGER
        )
        """
    )

    rows = []
    for pk in range(1, 31):
        provider = PHONE_PROVIDER if pk % 2 else FACETIME_PROVIDER
        mailbox_type = 2 if pk in DELETED_PKS else 1
        sender = FROM_ADDRESSES[pk % len(FROM_ADDRESSES)]
        dt = ANCHOR_TODAY - timedelta(minutes=(31 - pk))
        # First two rows exercise the "no transcript" / undecodable path;
        # every later row has a decodable transcript so the tail (the last
        # 20 by Z_PK, which is what the decode-ratio test samples) clears
        # its 80%-decodable threshold comfortably.
        transcript = (
            None
            if pk <= 2
            else _transcript_blob(f"This is fixture voicemail transcript number {pk}.")
        )
        record_uuid = _record_uuid_bytes(pk)
        rows.append(
            (
                pk,
                0,
                mailbox_type,
                2,
                _mac_seconds(dt),
                _mac_seconds(dt),
                float(5 + pk),
                sender,
                "+16125550100",
                provider,
                None,
                record_uuid,
                None,
                transcript,
                pk % 2,
            )
        )

    conn.executemany(
        "INSERT INTO ZSTOREDMESSAGE ("
        "Z_PK, ZMESSAGETYPE, ZMAILBOXTYPE, ZTRANSCRIPTIONSTATUS, ZDATECREATED, "
        "ZDATEMODIFIED, ZDURATION, ZFROM, ZRECIPIENT, ZPROVIDER, ZSIMID, "
        "ZRECORDUUID, ZCALLUUID, ZTRANSCRIPT, ZISREAD"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        rows,
    )
    conn.commit()
    conn.close()


def build_voicemail_assets_fixture(assets_dir: Path, pks: range = range(1, 31)) -> None:
    """Synthetic Assets/<UUID[0:2]>/<UUID>.m4a placeholder files matching the
    ZRECORDUUID values build_voicemail_fixture() writes for the same ``pks``,
    laid out the way ``VoicemailSource._resolve_audio_path`` expects. Content
    is a placeholder — only existence and path matter to the resolver."""
    for pk in pks:
        uuid_str = _record_uuid_str(pk)
        sub = assets_dir / uuid_str[:2].upper()
        sub.mkdir(parents=True, exist_ok=True)
        (sub / f"{uuid_str}.m4a").write_bytes(b"FIXTURE-PLACEHOLDER-AUDIO")
