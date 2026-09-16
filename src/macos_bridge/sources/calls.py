"""Calls source — watches CallHistory.storedata for both Phone and FaceTime.

Service classification is config-driven (substring match against ZSERVICE_PROVIDER).

Event paths (under <prefix>/<host>/comms/):
  - phone/started, phone/ended, phone/missed
  - facetime/started, facetime/ended, facetime/missed

CallHistory only logs the row after the call completes. "started" events are
emitted with the actual start timestamp (ZDATE) but delivered post-hoc, so
they are historical markers usable for reconstructing call timelines, NOT
real-time triggers. The Swift CXCallObserver subprocess provides the
real-time alternative on phone/{ringing,outgoing_started,connected,realtime_ended}.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable

from macos_bridge.config import CallsSource as CallsConfig
from macos_bridge.contacts import ContactResolver
from macos_bridge.db import fetch_max, open_ro
from macos_bridge.state import State
from macos_bridge.time_utils import mac_absolute_to_iso, mac_absolute_to_unix, unix_to_iso

log = logging.getLogger(__name__)

_BASE_SELECT = """
SELECT
    Z_PK                AS pk,
    ZDATE               AS date,
    ZDURATION           AS duration,
    ZADDRESS            AS address,
    ZSERVICE_PROVIDER   AS service,
    ZORIGINATED         AS originated,
    ZANSWERED           AS answered,
    ZCALLTYPE           AS call_type,
    ZNAME               AS name,
    ZUNIQUE_ID          AS unique_id,
    ZISO_COUNTRY_CODE   AS country_code,
    ZWASEMERGENCYCALL   AS was_emergency,
    ZHASMESSAGE         AS has_message,
    ZCONVERSATIONID     AS conversation_id
FROM ZCALLRECORD
"""

QUERY = _BASE_SELECT + " WHERE Z_PK > ? ORDER BY Z_PK ASC"

# For state-mirror seeding at startup: most-recent N rows in DESCENDING order.
RECENT_QUERY = _BASE_SELECT + " ORDER BY Z_PK DESC LIMIT ?"


class CallsSource:
    def __init__(
        self,
        cfg: CallsConfig,
        db_path: str,
        contacts: ContactResolver | None = None,
    ):
        self.cfg = cfg
        self.db_path = db_path
        self.contacts = contacts

    def init_state(self, state: State) -> None:
        try:
            max_id = fetch_max(self.db_path, "ZCALLRECORD", "Z_PK")
        except Exception as e:  # noqa: BLE001
            log.warning(
                "calls init-state could not read %s: %s — leaving unprimed",
                self.db_path, e,
            )
            return
        state.set("calls_last_pk", max_id)
        state.set("calls_primed", True)
        log.info("calls init-state primed at pk=%d", max_id)

    def poll(self, state: State) -> Iterable[tuple[str, dict]]:
        last = int(state.get("calls_last_pk", 0) or 0)
        primed = bool(state.get("calls_primed", False))
        try:
            with open_ro(self.db_path) as conn:
                rows = conn.execute(QUERY, (last,)).fetchall()
        except FileNotFoundError:
            log.warning("calls db not found at %s", self.db_path)
            return
        except Exception:
            log.exception("error reading calls db")
            return

        if not primed:
            new_max = max((int(r["pk"]) for r in rows), default=last)
            state.set("calls_last_pk", new_max)
            state.set("calls_primed", True)
            log.info(
                "calls source primed to pk=%d (skipping %d backlog rows)",
                new_max, len(rows),
            )
            return

        new_max = last
        for row in rows:
            pk = int(row["pk"])
            new_max = max(new_max, pk)
            yield from self._row_to_events(row, emit_started=self.cfg.emit_started_event)

        if new_max > last:
            state.set("calls_last_pk", new_max)

    def iter_recent(self, limit: int = 200) -> Iterable[tuple[str, dict]]:
        """For state-mirror seeding at startup: yield (event_path, payload)
        for the most recent ``limit`` calls in DESCENDING Z_PK order.
        Skips synthetic ``started`` events — only the terminal call event
        (``ended`` or ``missed``) is meaningful for HA's "most recent call"
        sensors. Does NOT update state."""
        try:
            with open_ro(self.db_path) as conn:
                rows = conn.execute(RECENT_QUERY, (int(limit),)).fetchall()
        except FileNotFoundError:
            log.warning("calls db not found at %s — skipping seed", self.db_path)
            return
        except Exception:
            log.exception("error reading calls db for seed")
            return
        for row in rows:
            yield from self._row_to_events(row, emit_started=False)

    def _row_to_events(self, row, *, emit_started: bool) -> Iterable[tuple[str, dict]]:
        pk = int(row["pk"])
        kind = self._classify(row["service"] or "")
        if kind is None:
            log.debug(
                "skipping call pk=%d with unrecognized service %r",
                pk, row["service"],
            )
            return

        address = _decode_address(row["address"])
        start_unix = mac_absolute_to_unix(row["date"])
        duration = float(row["duration"] or 0)
        end_unix = start_unix + duration
        originated = "outgoing" if row["originated"] else "incoming"
        answered = bool(row["answered"])

        base = {
            "pk": pk,
            "unique_id": row["unique_id"],
            "service": row["service"],
            "address": address,
            "name": row["name"],
            "country_code": row["country_code"],
            # Title-cased so HA renders the last_*_call_direction sensor
            # naturally as "Incoming" / "Outgoing".
            "direction": "Outgoing" if originated == "outgoing" else "Incoming",
            "answered": answered,
            "call_type": row["call_type"],
            "duration_seconds": duration,
            "started_at": mac_absolute_to_iso(row["date"]),
            "ended_at": unix_to_iso(end_unix),
            "was_emergency": bool(row["was_emergency"]),
            "has_message": bool(row["has_message"]),
            "conversation_id": row["conversation_id"],
        }
        contact = self.contacts.resolve_handle(address) if self.contacts else None
        if contact is not None:
            base["contact"] = contact.to_payload()
            # See messages.py for the __photo_bytes convention; the
            # runtime pops these and publishes them to the entity's
            # HA MQTT image topic.
            if contact.photo_bytes:
                base["__photo_bytes"] = contact.photo_bytes
                base["__photo_mime"] = contact.photo_mime

        if originated == "incoming" and not answered:
            yield f"{kind}/missed", base
            return

        if emit_started:
            yield f"{kind}/started", {**base, "event_phase": "started"}
        yield f"{kind}/ended", {**base, "event_phase": "ended"}

    def _classify(self, service: str) -> str | None:
        s = service.lower()
        for needle in self.cfg.facetime_service_substrings:
            if needle.lower() in s:
                return "facetime"
        for needle in self.cfg.phone_service_substrings:
            if needle.lower() in s:
                return "phone"
        return None


def _decode_address(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, (bytes, bytearray)):
        try:
            return value.decode("utf-8")
        except UnicodeDecodeError:
            return value.hex()
    return str(value)
