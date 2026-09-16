"""Voicemail source — watches FaceTimeMessageStore-local.sqlitedb.

On macOS 26, the new Phone app and FaceTime share a CoreData-backed SQLite
store at ~/Library/Group Containers/group.com.apple.FaceTime/
com.apple.facetimemessagestored/Data Store/FaceTimeMessageStore-local.sqlitedb.

There is NO ~/Library/Voicemail/ on macOS 26. ZSTOREDMESSAGE rows cover both
phone voicemails (ZPROVIDER=com.apple.coretelephony) and FaceTime audio
messages. Audio is in Assets/<UUID[0:2]>/<UUID>.{amr,m4a,...} keyed by
ZRECORDUUID. ZTRANSCRIPT is an NSKeyedArchiver bplist whose root dict has a
`transcriptionString` UID pointing into $objects.

Emits (under <prefix>/<host>/comms/):
  - voicemail/received                   for ZPROVIDER matching phone_provider_substrings
  - facetime/audio_message_received      for FaceTime audio messages

Filters out ZMAILBOXTYPE=2 (deleted) by default.
"""

from __future__ import annotations

import logging
import plistlib
from collections.abc import Iterable
from pathlib import Path

from macos_bridge.config import VoicemailSource as VoicemailConfig
from macos_bridge.contacts import ContactResolver
from macos_bridge.db import fetch_max, open_ro
from macos_bridge.state import State
from macos_bridge.time_utils import mac_absolute_to_iso

log = logging.getLogger(__name__)

_BASE_SELECT = """
SELECT
    Z_PK                  AS pk,
    ZMESSAGETYPE          AS message_type,
    ZMAILBOXTYPE          AS mailbox_type,
    ZTRANSCRIPTIONSTATUS  AS transcription_status,
    ZDATECREATED          AS date_created,
    ZDATEMODIFIED         AS date_modified,
    ZDURATION             AS duration,
    ZFROM                 AS sender,
    ZRECIPIENT            AS recipient,
    ZPROVIDER             AS provider,
    ZSIMID                AS sim_id,
    ZRECORDUUID           AS record_uuid,
    ZCALLUUID             AS call_uuid,
    ZTRANSCRIPT           AS transcript_blob,
    ZISREAD               AS is_read
FROM ZSTOREDMESSAGE
"""

QUERY = _BASE_SELECT + " WHERE Z_PK > ? ORDER BY Z_PK ASC"

# For state-mirror seeding at startup: most-recent N rows in DESCENDING order.
RECENT_QUERY = _BASE_SELECT + " ORDER BY Z_PK DESC LIMIT ?"


class VoicemailSource:
    def __init__(
        self,
        cfg: VoicemailConfig,
        db_path: str,
        assets_dir: str | None,
        contacts: ContactResolver | None = None,
    ):
        self.cfg = cfg
        self.db_path = db_path
        self.assets_dir = Path(assets_dir) if assets_dir else None
        self.contacts = contacts

    def init_state(self, state: State) -> None:
        try:
            max_pk = fetch_max(self.db_path, "ZSTOREDMESSAGE", "Z_PK")
        except Exception as e:  # noqa: BLE001
            log.warning(
                "voicemail init-state could not read %s: %s — leaving unprimed",
                self.db_path, e,
            )
            return
        state.set("voicemail_last_pk", max_pk)
        state.set("voicemail_primed", True)
        log.info("voicemail init-state primed at pk=%d", max_pk)

    def poll(self, state: State) -> Iterable[tuple[str, dict]]:
        last = int(state.get("voicemail_last_pk", 0) or 0)
        primed = bool(state.get("voicemail_primed", False))

        try:
            with open_ro(self.db_path) as conn:
                rows = conn.execute(QUERY, (last,)).fetchall()
        except FileNotFoundError:
            log.warning("voicemail db not found at %s", self.db_path)
            return
        except Exception:
            log.exception("error reading voicemail db")
            return

        if not primed:
            new_max = max((int(r["pk"]) for r in rows), default=last)
            state.set("voicemail_last_pk", new_max)
            state.set("voicemail_primed", True)
            log.info(
                "voicemail source primed to pk=%d (skipping %d backlog rows)",
                new_max, len(rows),
            )
            return

        new_max = last
        for row in rows:
            pk = int(row["pk"])
            new_max = max(new_max, pk)
            event = self._row_to_event(row)
            if event is not None:
                yield event

        if new_max > last:
            state.set("voicemail_last_pk", new_max)

    def iter_recent(self, limit: int = 200) -> Iterable[tuple[str, dict]]:
        """For state-mirror seeding at startup: yield (event_path, payload)
        for the most recent ``limit`` voicemails / FaceTime audio messages
        in DESCENDING Z_PK order. Does NOT update state."""
        try:
            with open_ro(self.db_path) as conn:
                rows = conn.execute(RECENT_QUERY, (int(limit),)).fetchall()
        except FileNotFoundError:
            log.warning("voicemail db not found at %s — skipping seed", self.db_path)
            return
        except Exception:
            log.exception("error reading voicemail db for seed")
            return
        for row in rows:
            event = self._row_to_event(row)
            if event is not None:
                yield event

    def _row_to_event(self, row) -> tuple[str, dict] | None:
        pk = int(row["pk"])
        mbox = int(row["mailbox_type"] or 0)
        if mbox == 2 and not self.cfg.include_deleted:
            return None

        kind = self._classify_provider(row["provider"] or "")
        if kind is None:
            log.debug(
                "skipping voicemail pk=%d with unrecognized provider %r",
                pk, row["provider"],
            )
            return None

        uuid_str = _format_uuid(row["record_uuid"])
        audio_path = self._resolve_audio_path(uuid_str) if uuid_str else None
        transcription = _decode_transcript(row["transcript_blob"])

        payload = {
            "pk": pk,
            "uuid": uuid_str,
            "provider": row["provider"],
            "kind": kind,
            "sender": row["sender"],
            "recipient": row["recipient"],
            "duration_seconds": float(row["duration"] or 0),
            "timestamp": mac_absolute_to_iso(row["date_created"]),
            "modified_at": mac_absolute_to_iso(row["date_modified"]),
            "message_type": int(row["message_type"] or 0),
            "mailbox_type": mbox,
            "transcription_status": int(row["transcription_status"] or 0),
            "transcription": transcription,
            "is_read": bool(row["is_read"]),
            "sim_id": row["sim_id"],
        }
        if self.cfg.include_audio_path:
            payload["audio_path"] = audio_path

        contact = self.contacts.resolve_handle(row["sender"]) if self.contacts else None
        if contact is not None:
            payload["contact"] = contact.to_payload()
            # See messages.py for the __photo_bytes convention; the
            # runtime pops these and publishes them to the entity's
            # HA MQTT image topic.
            if contact.photo_bytes:
                payload["__photo_bytes"] = contact.photo_bytes
                payload["__photo_mime"] = contact.photo_mime

        event_path = (
            "voicemail/received"
            if kind == "phone"
            else "facetime/audio_message_received"
        )
        return event_path, payload

    def _classify_provider(self, provider: str) -> str | None:
        s = provider.lower()
        for needle in self.cfg.facetime_provider_substrings:
            if needle.lower() in s:
                return "facetime"
        for needle in self.cfg.phone_provider_substrings:
            if needle.lower() in s:
                return "phone"
        return None

    def _resolve_audio_path(self, uuid_str: str) -> str | None:
        if not self.assets_dir or not self.assets_dir.exists():
            return None
        sub = self.assets_dir / uuid_str[:2].upper()
        if not sub.exists():
            return None
        for ext in self.cfg.audio_extensions:
            p = sub / f"{uuid_str}.{ext}"
            if p.exists():
                return str(p)
        return None


def _format_uuid(blob) -> str | None:
    if not blob or not isinstance(blob, (bytes, bytearray)) or len(blob) != 16:
        return None
    h = blob.hex().upper()
    return f"{h[0:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:32]}"


def _decode_transcript(blob) -> str | None:
    """ZTRANSCRIPT is an NSKeyedArchiver bplist with one of two shapes:

      Shape A — single transcription:
        root = {transcriptionString: UID, confidence: ..., ...}
        join: objects[root['transcriptionString']]

      Shape B — segmented transcription (NSArray of utterance dicts):
        root = {NS.objects: [UID, UID, ...], $class: NSArray}
        each UID points to a dict with `text`, `confidence`, `utteranceDuration`
        join: concatenate text fields in order
    """
    if not blob:
        return None
    try:
        plist = plistlib.loads(blob)
    except Exception:  # noqa: BLE001
        return None

    objects = plist.get("$objects")
    top = plist.get("$top")
    if not isinstance(objects, list) or not isinstance(top, dict):
        return None

    def _resolve(uid):
        if not hasattr(uid, "data"):
            return None
        idx = uid.data
        return objects[idx] if 0 <= idx < len(objects) else None

    root = _resolve(top.get("root"))
    if not isinstance(root, dict):
        return None

    if "transcriptionString" in root:
        text = _resolve(root["transcriptionString"])
        return text if isinstance(text, str) else None

    ns_objs = root.get("NS.objects")
    if isinstance(ns_objs, list):
        parts: list[str] = []
        for u in ns_objs:
            seg = _resolve(u)
            if not isinstance(seg, dict):
                continue
            txt = _resolve(seg.get("text")) if "text" in seg else None
            if isinstance(txt, str):
                parts.append(txt)
        if parts:
            return " ".join(parts).strip() or None

    return None
