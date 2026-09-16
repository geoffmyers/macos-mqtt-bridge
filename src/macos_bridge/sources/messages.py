"""Messages source — watches ~/Library/Messages/chat.db.

Emits (under <prefix>/<host>/comms/):
  - messages/sent             a message was sent from this Mac
  - messages/received         a message was received
  - messages/reaction         tapback added or removed
  - messages/edited           edit event on an existing message
  - messages/retracted        retract event
  - messages/group_event      participant add/remove, name/photo change

Detects new rows by tracking the max ROWID of the `message` table.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable

from macos_bridge.config import MessagesSource as MessagesConfig
from macos_bridge.contacts import ContactResolver
from macos_bridge.db import fetch_max, open_ro
from macos_bridge.state import State
from macos_bridge.time_utils import mac_absolute_to_iso

log = logging.getLogger(__name__)

_BASE_SELECT = """
SELECT
    m.ROWID                     AS rowid,
    m.guid                      AS guid,
    m.text                      AS text,
    m.attributedBody            AS attributed_body,
    m.is_from_me                AS is_from_me,
    m.date                      AS date,
    m.date_edited               AS date_edited,
    m.date_retracted            AS date_retracted,
    m.service                   AS service,
    m.associated_message_type   AS assoc_type,
    m.associated_message_guid   AS assoc_guid,
    m.associated_message_emoji  AS assoc_emoji,
    m.item_type                 AS item_type,
    m.group_action_type         AS group_action_type,
    m.group_title               AS group_title,
    m.other_handle              AS other_handle,
    m.cache_has_attachments     AS has_attachments,
    h.id                        AS handle,
    c.guid                      AS chat_guid,
    c.style                     AS chat_style,
    c.display_name              AS chat_display_name
FROM message m
LEFT JOIN handle h ON m.handle_id = h.ROWID
LEFT JOIN chat_message_join cmj ON m.ROWID = cmj.message_id
LEFT JOIN chat c ON cmj.chat_id = c.ROWID
"""

QUERY = _BASE_SELECT + " WHERE m.ROWID > ? ORDER BY m.ROWID ASC"

# For state-mirror seeding at startup: most-recent N rows in DESCENDING order.
RECENT_QUERY = _BASE_SELECT + " ORDER BY m.ROWID DESC LIMIT ?"

# associated_message_type values for tapbacks. Apple uses:
#   2000-2006: added reaction
#   3000-3005: removed reaction
# Names are Title-cased so they read naturally as HA entity states
# ("Loved" rather than "loved").
REACTION_NAMES = {
    0: "Loved",
    1: "Liked",
    2: "Disliked",
    3: "Laughed",
    4: "Emphasized",
    5: "Questioned",
    6: "Sticker",
}


class MessagesSource:
    def __init__(
        self,
        cfg: MessagesConfig,
        db_path: str,
        contacts: ContactResolver | None = None,
    ):
        self.cfg = cfg
        self.db_path = db_path
        self.contacts = contacts

    def init_state(self, state: State) -> None:
        try:
            max_id = fetch_max(self.db_path, "message", "ROWID")
        except Exception as e:  # noqa: BLE001
            log.warning(
                "messages init-state could not read %s: %s — leaving unprimed",
                self.db_path, e,
            )
            return
        state.set("messages_last_rowid", max_id)
        state.set("messages_primed", True)
        log.info("messages init-state primed at rowid=%d", max_id)

    def poll(self, state: State) -> Iterable[tuple[str, dict]]:
        last = int(state.get("messages_last_rowid", 0) or 0)
        primed = bool(state.get("messages_primed", False))
        try:
            with open_ro(self.db_path) as conn:
                rows = conn.execute(QUERY, (last,)).fetchall()
        except FileNotFoundError:
            log.warning(
                "messages db not found at %s — disabling source until next poll",
                self.db_path,
            )
            return
        except Exception:
            log.exception("error reading messages db")
            return

        if not primed:
            new_max = max((int(r["rowid"]) for r in rows), default=last)
            state.set("messages_last_rowid", new_max)
            state.set("messages_primed", True)
            log.info(
                "messages source primed to rowid=%d (skipping %d backlog rows)",
                new_max, len(rows),
            )
            return

        new_max = last
        for row in rows:
            rowid = int(row["rowid"])
            new_max = max(new_max, rowid)
            yield self._row_to_event(row)

        if new_max > last:
            state.set("messages_last_rowid", new_max)

    def iter_recent(self, limit: int = 200) -> Iterable[tuple[str, dict]]:
        """For state-mirror seeding at startup: yield (event_path, payload)
        for the most recent ``limit`` messages in DESCENDING ROWID order.
        Does NOT update state — this is a read-only scan whose only side
        effect is the publishes the bridge does to retained state-mirror
        topics."""
        try:
            with open_ro(self.db_path) as conn:
                rows = conn.execute(RECENT_QUERY, (int(limit),)).fetchall()
        except FileNotFoundError:
            log.warning("messages db not found at %s — skipping seed", self.db_path)
            return
        except Exception:
            log.exception("error reading messages db for seed")
            return
        for row in rows:
            yield self._row_to_event(row)

    def _row_to_event(self, row) -> tuple[str, dict]:
        text = row["text"]
        if not text and self.cfg.include_attributed_body_fallback:
            text = _extract_attributed_text(row["attributed_body"])
        base = {
            "rowid": int(row["rowid"]),
            "guid": row["guid"],
            "service": row["service"],  # iMessage / SMS / RCS
            "handle": row["handle"],
            "is_from_me": bool(row["is_from_me"]),
            "timestamp": mac_absolute_to_iso(row["date"]),
            "has_attachments": bool(row["has_attachments"]),
            "chat": {
                "guid": row["chat_guid"],
                "is_group": row["chat_style"] == 43,
                "display_name": row["chat_display_name"],
            },
        }
        contact = self.contacts.resolve_handle(row["handle"]) if self.contacts else None
        if contact is not None:
            base["contact"] = contact.to_payload()
            # Stash binary photo bytes under a double-underscore-prefixed
            # key. The runtime pops these before JSON-serializing the
            # event payload and republishes the bytes to the entity's
            # HA MQTT image topic.
            if contact.photo_bytes:
                base["__photo_bytes"] = contact.photo_bytes
                base["__photo_mime"] = contact.photo_mime
        event_path, extra = _classify(row, text, self.cfg.include_text)
        return event_path, {**base, **extra}


def _classify(row, text: str | None, include_text: bool) -> tuple[str, dict]:
    """Decide which sub-topic this row maps to, and return any extra fields."""
    if row["item_type"]:
        return (
            "messages/group_event",
            {
                "item_type": row["item_type"],
                "group_action_type": row["group_action_type"],
                "group_title": row["group_title"],
                "other_handle": row["other_handle"],
            },
        )

    assoc_type = row["assoc_type"] or 0
    if 2000 <= assoc_type < 3100:
        is_remove = assoc_type >= 3000
        kind_idx = assoc_type % 1000
        return (
            "messages/reaction",
            {
                "reaction_kind": REACTION_NAMES.get(kind_idx, f"Unknown ({kind_idx})"),
                "is_remove": is_remove,
                "target_guid": _strip_part_prefix(row["assoc_guid"]),
                "emoji": row["assoc_emoji"],
            },
        )

    extra: dict = {}
    if include_text:
        extra["text"] = text or None

    if row["date_retracted"]:
        extra["retracted_at"] = mac_absolute_to_iso(row["date_retracted"])
        return "messages/retracted", extra
    if row["date_edited"]:
        extra["edited_at"] = mac_absolute_to_iso(row["date_edited"])
        return "messages/edited", extra

    return ("messages/sent" if row["is_from_me"] else "messages/received"), extra


def _strip_part_prefix(guid: str | None) -> str | None:
    """Reaction associated_message_guid is `p:0/<original-guid>`."""
    if not guid:
        return None
    if "/" in guid:
        return guid.split("/", 1)[1]
    return guid


def _extract_attributed_text(blob: bytes | None) -> str | None:
    """Best-effort plain-text extraction from a chat.db attributedBody blob.

    attributedBody is an NSKeyedArchiver typedstream. We scan for the NSString
    marker and then read either a short (<128 byte) or 2-byte-length string.
    Falls back to None on anything more exotic.
    """
    if not blob:
        return None
    marker = b"NSString"
    idx = blob.find(marker)
    if idx == -1:
        return None
    pos = idx + len(marker)
    end = len(blob)
    while pos < end:
        b = blob[pos]
        if b == 0x01 or b == 0x84 or b == 0x94 or b == 0x2B:
            pos += 1
            continue
        if b < 0x80:
            length = b
            pos += 1
            text_bytes = blob[pos : pos + length]
            try:
                return text_bytes.decode("utf-8")
            except UnicodeDecodeError:
                return None
        if b == 0x81:
            if pos + 3 > end:
                return None
            length = int.from_bytes(blob[pos + 1 : pos + 3], "little")
            pos += 3
            text_bytes = blob[pos : pos + length]
            try:
                return text_bytes.decode("utf-8")
            except UnicodeDecodeError:
                return None
        pos += 1
    return None
