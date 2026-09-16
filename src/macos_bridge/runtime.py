"""Merged daemon runtime.

Owns one MQTT connection (``MqttPublisher``) shared by:

  - The comms event sources (Messages, Calls, Voicemail), polled every
    ``bridge.poll_interval_seconds`` from a worker thread; events go through
    a disk-backed outbox so a broker outage doesn't drop them.
  - The Swift CXCallObserver subprocess, which streams real-time call state.
  - The outbound osascript handler, subscribed to
    ``<prefix>/<host>/comms/messages/send``.
  - The seven phase tickers (a–g), each running its own asyncio task with
    its own cadence (or self-paced for Phase C).

The two halves coexist in one process: the asyncio supervisor runs in the
main thread; the comms polling loop runs in a daemon thread; the CallKit
helper runs as a subprocess. Stop signals (SIGINT/SIGTERM) cancel the
asyncio supervisor, which in turn signals the polling thread to drain
and exit.
"""

from __future__ import annotations

import asyncio
import logging
import logging.handlers
import signal
import sys
import threading
import time
from pathlib import Path
from zoneinfo import ZoneInfo

from macos_bridge.audio_hijack import AudioHijackController
from macos_bridge.config import Config, LoadedCredentials
from macos_bridge.contacts import ContactResolver, discover_source_dbs, empty_resolver
from macos_bridge.controls import ControlsHandler
from macos_bridge.discovery import (
    DERIVED_ENTITIES,
    ENTITIES,
    IMAGE_ENTITIES,
    CommsHADiscovery,
)
from macos_bridge.mqtt import MqttPublisher
from ha_mqtt_bridge import Outbox

from macos_bridge.outbound import OutboundHandler, SendRequest
from macos_bridge.phases.base import AbstractTicker
from macos_bridge.phases.aggregates import AggregatesTicker
from macos_bridge.phases.per_app import PerAppTicker
from macos_bridge.phases.focused_app import FocusedAppTicker
from macos_bridge.phases.brave_history import BraveHistoryTicker
from macos_bridge.phases.family import FamilyTicker
from macos_bridge.phases.activity import ActivityTicker
from macos_bridge.phases.system_state import SystemStateTicker
from macos_bridge.phases.today_counters import TodayCountersTicker
from macos_bridge.phases.displays import DisplaysTicker
from macos_bridge.phases.focus_mode import FocusModeTicker
from macos_bridge.phases.now_playing import NowPlayingTicker
from macos_bridge.phases.permissions import PermissionsTicker
from macos_bridge.phases.security_posture import SecurityPostureTicker
from macos_bridge.phases.software_updates import SoftwareUpdatesTicker
from macos_bridge.phases.location import LocationTicker
from macos_bridge.phases.system_info import SystemInfoTicker
from macos_bridge.phases.tailscale import TailscaleTicker
from macos_bridge.phases.time_machine import TimeMachineTicker
from macos_bridge.phases.unread_messages import UnreadMessagesTicker
from macos_bridge.phases.virtual_meetings import VirtualMeetingsTicker
from macos_bridge.phases.host_metrics import HostMetricsTicker
from macos_bridge.realtime_calls import RealtimeCallObserver
from macos_bridge.sources.calls import CallsSource
from macos_bridge.sources.messages import MessagesSource
from macos_bridge.sources.voicemail import VoicemailSource
from macos_bridge.state import State

log = logging.getLogger(__name__)

__version__ = "0.1.0"


def configure_logging(log_path: str, level: str) -> None:
    """Configure root logger with file + stderr handlers."""
    resolved = Path(log_path).expanduser()
    resolved.parent.mkdir(parents=True, exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(
        resolved, maxBytes=10 * 1024 * 1024, backupCount=7
    )
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    handler.setFormatter(fmt)
    root = logging.getLogger()
    root.addHandler(handler)
    root.setLevel(level.upper())
    stream = logging.StreamHandler(sys.stderr)
    stream.setFormatter(fmt)
    root.addHandler(stream)


class Bridge:
    """The unified macos-mqtt-bridge daemon."""

    def __init__(
        self,
        cfg: Config,
        creds: LoadedCredentials | None,
        *,
        host_slug: str,
        host_friendly_name: str | None = None,
        serial_number: str | None = None,
        mac_address: str | None = None,
        dry_run: bool = False,
    ) -> None:
        self.cfg = cfg
        self._creds = creds
        self._host_slug = host_slug
        self._host_friendly_name = host_friendly_name
        self._serial_number = serial_number
        self._mac_address = mac_address
        self.dry_run = dry_run

        self._stop = threading.Event()
        self._comms_thread: threading.Thread | None = None
        self._discovery_published = False

        paths = cfg.expanded_paths()
        self._paths = paths
        self.state = State(paths["state_path"])

        self.publisher: MqttPublisher | None = None
        self.outbox: Outbox | None = None
        if not dry_run:
            self.publisher = MqttPublisher(
                cfg.mqtt,
                host_slug=host_slug,
                sw_version=__version__,
                host_friendly_name=host_friendly_name,
                serial_number=serial_number,
                mac_address=mac_address,
            )
            outbox_path = str(Path(paths["state_path"]).parent / "outbox.jsonl")
            self.outbox = Outbox(outbox_path)

        self.contacts: ContactResolver = self._build_contact_resolver(cfg, paths)

        # Discovery helper for the comms half; built once we know the device block.
        self.comms_discovery: CommsHADiscovery | None = None

        self.outbound: OutboundHandler | None = (
            OutboundHandler(osascript_path=cfg.outbound.osascript_path)
            if cfg.outbound.enabled and not dry_run
            else None
        )

        self.realtime_calls: RealtimeCallObserver | None = None
        if cfg.realtime_calls.enabled and not dry_run:
            self.realtime_calls = RealtimeCallObserver(
                binary_path=cfg.realtime_calls.binary_path,
                emit=self._emit,
            )

        # Inbound HA controls — built lazily once the publisher exists
        # (it needs ``MqttPublisher.host_device_block()`` for discovery
        # payloads). The actual subscribe + discovery publish happens
        # in ``_run_async`` after the broker connects.
        self.controls_handler: ControlsHandler | None = None
        if cfg.controls.enabled and not dry_run and self.publisher is not None:
            self.controls_handler = ControlsHandler(
                cfg=cfg,
                host_slug=host_slug,
                publisher=self.publisher,
            )

        self._audio_hijack: AudioHijackController | None = None
        if cfg.audio_hijack.enabled and not dry_run:
            self._audio_hijack = AudioHijackController(
                zoom_session=cfg.audio_hijack.zoom_session,
                teams_session=cfg.audio_hijack.teams_session,
                facetime_session=cfg.audio_hijack.facetime_session,
                browser_session=cfg.audio_hijack.browser_session,
                phone_session=cfg.audio_hijack.phone_session,
                auto_stop=cfg.audio_hijack.auto_stop,
            )

        self.sources: list = []
        if cfg.sources.messages.enabled:
            self.sources.append(
                MessagesSource(
                    cfg.sources.messages, paths["messages_db"], self.contacts
                )
            )
        if cfg.sources.calls.enabled:
            self.sources.append(
                CallsSource(cfg.sources.calls, paths["calls_db"], self.contacts)
            )
        if cfg.sources.voicemail.enabled:
            self.sources.append(
                VoicemailSource(
                    cfg.sources.voicemail,
                    paths["voicemail_db"],
                    paths["voicemail_assets_dir"],
                    self.contacts,
                )
            )

        self.tickers: list[AbstractTicker] = self._build_tickers(cfg, paths)

    # ---- builders -----------------------------------------------------------

    @staticmethod
    def _build_contact_resolver(cfg: Config, paths: dict) -> ContactResolver:
        if not cfg.contacts.enabled:
            return empty_resolver()
        ab_dir = paths["address_book_dir"]
        if not ab_dir:
            return empty_resolver()
        source_dbs = discover_source_dbs(ab_dir)
        if not source_dbs:
            log.warning("contacts enabled but no AddressBook source DBs found under %s", ab_dir)
            return empty_resolver()
        return ContactResolver(
            source_dbs,
            include_photo=cfg.contacts.include_photo,
            photo_field=cfg.contacts.photo_field,
        )

    def _build_tickers(self, cfg: Config, paths: dict) -> list[AbstractTicker]:
        host_slug = self._host_slug
        topic_prefix = cfg.mqtt.topic_prefix
        availability = cfg.mqtt.lwt_topic
        tmp_dir = Path(paths["tmp_dir"])
        knowledge_db = Path(paths["knowledge_db"]) if paths["knowledge_db"] else None
        rm_admin_local = Path(paths["rm_admin_local"]) if paths["rm_admin_local"] else None
        local_tz: ZoneInfo | None = None  # default to system tz inside helpers

        tickers: list[AbstractTicker] = [
            AggregatesTicker(
                enabled=cfg.aggregates.enabled,
                interval_seconds=cfg.aggregates.interval_seconds,
                knowledge_db_path=knowledge_db or Path(),
                tmp_dir=tmp_dir,
                host_slug=host_slug,
                topic_prefix=topic_prefix,
                availability_topic=availability,
                local_timezone=local_tz,
            ),
            PerAppTicker(
                enabled=cfg.per_app.enabled,
                interval_seconds=cfg.per_app.interval_seconds,
                knowledge_db_path=knowledge_db or Path(),
                tmp_dir=tmp_dir,
                host_slug=host_slug,
                topic_prefix=topic_prefix,
                availability_topic=availability,
                apps=list(cfg.per_app.apps),
                local_timezone=local_tz,
            ),
            FocusedAppTicker(
                enabled=cfg.focused_app.enabled,
                poll_interval_seconds=cfg.focused_app.poll_interval_seconds,
                knowledge_db_path=knowledge_db,
                tmp_dir=tmp_dir,
                host_slug=host_slug,
                topic_prefix=topic_prefix,
                availability_topic=availability,
                publish_locked_state=cfg.focused_app.publish_locked_state,
            ),
            BraveHistoryTicker(
                enabled=cfg.brave_history.enabled,
                interval_seconds=cfg.brave_history.interval_seconds,
                brave_home=(
                    Path(paths["brave_home"]) if paths.get("brave_home") else None
                ),
                tmp_dir=tmp_dir,
                host_slug=host_slug,
                topic_prefix=topic_prefix,
                availability_topic=availability,
                local_timezone=local_tz,
                max_visits_per_profile=cfg.brave_history.max_visits_per_profile,
            ),
            FamilyTicker(
                enabled=cfg.family.enabled,
                is_family_organizer=cfg.family.is_family_organizer,
                interval_seconds=cfg.family.interval_seconds,
                rm_admin_local_path=rm_admin_local or Path(),
                tmp_dir=tmp_dir,
                topic_prefix=topic_prefix,
                availability_topic=availability,
                member_filter=list(cfg.family.members) if cfg.family.members else None,
                local_timezone=local_tz,
            ),
            ActivityTicker(
                enabled=cfg.activity.enabled,
                interval_seconds=cfg.activity.interval_seconds,
                host_slug=host_slug,
                topic_prefix=topic_prefix,
                availability_topic=availability,
                input_active_threshold_seconds=cfg.activity.input_active_threshold_seconds,
                camera_log_window_seconds=cfg.activity.camera_log_window_seconds,
                microphone_log_window_seconds=cfg.activity.microphone_log_window_seconds,
            ),
            SystemStateTicker(
                enabled=cfg.system_state.enabled,
                interval_seconds=cfg.system_state.interval_seconds,
                host_slug=host_slug,
                topic_prefix=topic_prefix,
                availability_topic=availability,
                wifi_interface=cfg.system_state.wifi_interface,
            ),
            HostMetricsTicker(
                enabled=cfg.host_metrics.enabled,
                interval_seconds=cfg.host_metrics.interval_seconds,
                host_slug=host_slug,
                topic_prefix=topic_prefix,
                availability_topic=availability,
                disk_mount=cfg.host_metrics.disk_mount,
            ),
            TodayCountersTicker(
                enabled=cfg.today_counters.enabled,
                interval_seconds=cfg.today_counters.interval_seconds,
                host_slug=host_slug,
                topic_prefix=topic_prefix,
                availability_topic=availability,
                messages_db_path=paths.get("messages_db"),
                messages_cfg=cfg.sources.messages,
                calls_db_path=paths.get("calls_db"),
                calls_cfg=cfg.sources.calls,
                voicemail_db_path=paths.get("voicemail_db"),
                voicemail_cfg=cfg.sources.voicemail,
                local_timezone=local_tz,
            ),
            VirtualMeetingsTicker(
                enabled=cfg.virtual_meetings.enabled,
                interval_seconds=cfg.virtual_meetings.interval_seconds,
                host_slug=host_slug,
                topic_prefix=topic_prefix,
                availability_topic=availability,
                end_grace_ticks=cfg.virtual_meetings.end_grace_ticks,
                on_meeting_started=(
                    self._audio_hijack.on_meeting_started
                    if self._audio_hijack else None
                ),
                on_meeting_ended=(
                    self._audio_hijack.on_meeting_ended
                    if self._audio_hijack else None
                ),
            ),
            PermissionsTicker(
                enabled=cfg.permissions.enabled,
                interval_seconds=cfg.permissions.interval_seconds,
                host_slug=host_slug,
                topic_prefix=topic_prefix,
                availability_topic=availability,
                # Location Services TCC is keyed on the LocationFetcher.app
                # helper bundle, not the daemon. Pass the same binary the
                # location phase ticker uses so the permissions probe
                # reports the *helper's* grant rather than the daemon's
                # (which never holds Location Services).
                location_binary_path=cfg.location.binary_path,
            ),
            FocusModeTicker(
                enabled=cfg.focus_mode.enabled,
                interval_seconds=cfg.focus_mode.interval_seconds,
                host_slug=host_slug,
                topic_prefix=topic_prefix,
                availability_topic=availability,
            ),
            UnreadMessagesTicker(
                enabled=cfg.unread_messages.enabled,
                interval_seconds=cfg.unread_messages.interval_seconds,
                host_slug=host_slug,
                topic_prefix=topic_prefix,
                availability_topic=availability,
                messages_db_path=paths.get("messages_db"),
                messages_cfg=cfg.sources.messages,
            ),
            SoftwareUpdatesTicker(
                enabled=cfg.software_updates.enabled,
                interval_seconds=cfg.software_updates.interval_seconds,
                host_slug=host_slug,
                topic_prefix=topic_prefix,
                availability_topic=availability,
            ),
            TailscaleTicker(
                enabled=cfg.tailscale.enabled,
                interval_seconds=cfg.tailscale.interval_seconds,
                host_slug=host_slug,
                topic_prefix=topic_prefix,
                availability_topic=availability,
            ),
            TimeMachineTicker(
                enabled=cfg.time_machine.enabled,
                interval_seconds=cfg.time_machine.interval_seconds,
                host_slug=host_slug,
                topic_prefix=topic_prefix,
                availability_topic=availability,
            ),
            DisplaysTicker(
                enabled=cfg.displays.enabled,
                interval_seconds=cfg.displays.interval_seconds,
                host_slug=host_slug,
                topic_prefix=topic_prefix,
                availability_topic=availability,
            ),
            SecurityPostureTicker(
                enabled=cfg.security_posture.enabled,
                interval_seconds=cfg.security_posture.interval_seconds,
                host_slug=host_slug,
                topic_prefix=topic_prefix,
                availability_topic=availability,
            ),
            NowPlayingTicker(
                enabled=cfg.now_playing.enabled,
                interval_seconds=cfg.now_playing.interval_seconds,
                host_slug=host_slug,
                topic_prefix=topic_prefix,
                availability_topic=availability,
            ),
            SystemInfoTicker(
                enabled=cfg.system_info.enabled,
                interval_seconds=cfg.system_info.interval_seconds,
                host_slug=host_slug,
                topic_prefix=topic_prefix,
                availability_topic=availability,
            ),
            LocationTicker(
                enabled=cfg.location.enabled,
                interval_seconds=cfg.location.interval_seconds,
                host_slug=host_slug,
                topic_prefix=topic_prefix,
                availability_topic=availability,
                binary_path=cfg.location.binary_path,
                fetch_timeout_seconds=cfg.location.fetch_timeout_seconds,
                home_latitude=cfg.location.home_latitude,
                home_longitude=cfg.location.home_longitude,
                home_radius_meters=cfg.location.home_radius_meters,
            ),
        ]
        return tickers

    # ---- comms event topics -------------------------------------------------

    def _comms_event_topic(self, event_path: str) -> str:
        # Comms event topics are flat under <prefix>/<host>/ — there's no
        # `comms/` infix. Phase ticker scopes (today/, focus/, activity/,
        # system/, host/, visible_apps_count) are disjoint from the comms
        # event-path roots (messages/, phone/, facetime/, voicemail/,
        # state/), so there's no collision.
        return f"{self.cfg.mqtt.topic_prefix}/{self._host_slug}/{event_path}"

    def _emit(self, event_path: str, payload: dict) -> None:
        """Per-event emit from a comms source (or the realtime call observer).
        Goes through the outbox so broker outages don't drop events.
        """
        if self._audio_hijack is not None:
            self._audio_hijack.on_phone_event(event_path)
        topic = self._comms_event_topic(event_path)
        # Extract binary contact-photo bytes BEFORE building the JSON
        # payload — bytes can't be serialized to JSON and they belong
        # on a dedicated MQTT image topic anyway.
        photo_bytes = payload.pop("__photo_bytes", None)
        photo_mime = payload.pop("__photo_mime", None)
        enriched = {"host": self._host_slug, **payload}
        if self.dry_run:
            log.info("[dry-run] %s %s", topic, enriched)
            return
        assert self.publisher is not None
        assert self.outbox is not None
        self.outbox.enqueue(topic, enriched)

        # Mirror to the HA state topic (retained) so the sensor's last value
        # survives a bridge restart.
        entity = None
        if self.cfg.mqtt.discovery_enabled and self.comms_discovery is not None:
            entity = self.comms_discovery.entity_for(event_path)
            if entity is not None:
                self.outbox.enqueue(
                    self.comms_discovery.state_topic(entity),
                    enriched,
                    retain=True,
                )

        # If this entity has a paired HA Image entity and the contact had
        # a photo, publish the raw bytes to the image topic (retained so
        # HA shows the last known photo even when the bridge is offline).
        # Bypasses the outbox — losing one photo on a broker hiccup is
        # fine; the next message of the same kind will republish.
        if (
            photo_bytes is not None
            and entity is not None
            and self.comms_discovery is not None
        ):
            image_topic = self.comms_discovery.image_topic_for_parent(entity.slug)
            if image_topic is not None:
                self.publisher.publish_raw(
                    image_topic, photo_bytes, qos=1, retain=True
                )
                log.debug(
                    "published %d-byte %s contact photo to %s",
                    len(photo_bytes), photo_mime or "image/?", image_topic,
                )

    def _drain_outbox(self) -> None:
        if self.outbox is None or self.publisher is None:
            return
        if not self.publisher.is_connected():
            return
        if self.cfg.mqtt.discovery_enabled and not self._discovery_published:
            self._publish_comms_discovery()
        drained = self.outbox.drain(self._publish_drained)
        if drained:
            log.debug("outbox drained %d event(s)", drained)

    def _publish_drained(self, topic: str, payload: dict, retain: bool | None) -> bool:
        assert self.publisher is not None
        return self.publisher.publish_event_with_ack(
            topic, payload, retain=retain, timeout=2.0
        )

    def _publish_comms_discovery(self) -> None:
        assert self.publisher is not None
        if self.comms_discovery is None:
            self.comms_discovery = CommsHADiscovery(
                cfg=self.cfg,
                hostname=self._host_slug,
                device_block=self.publisher.host_device_block(),
            )
        # Online binary sensor mirrors the LWT.
        online_topic, online_cfg = self.comms_discovery.binary_sensor_online()
        self.publisher.publish_event_with_ack(
            online_topic, online_cfg, retain=True, timeout=2.0
        )
        for entity in ENTITIES:
            self.publisher.publish_event_with_ack(
                self.comms_discovery.discovery_topic(entity),
                self.comms_discovery.discovery_config(entity),
                retain=True,
                timeout=2.0,
            )
        # Derived entities (timestamp/text/duration/direction extracts of
        # the primary state-mirror payloads) are published as ordinary HA
        # discovery configs that point at the parent's state_topic.
        for derived in DERIVED_ENTITIES:
            self.publisher.publish_event_with_ack(
                self.comms_discovery.derived_discovery_topic(derived),
                self.comms_discovery.derived_discovery_config(derived),
                retain=True,
                timeout=2.0,
            )
        # Image entities — one HA MQTT image per primary entity that has
        # a contact (everything except group_event). Bytes are published
        # by _emit / _seed_state_mirrors when an event with a contact
        # photo fires.
        for image in IMAGE_ENTITIES:
            self.publisher.publish_event_with_ack(
                self.comms_discovery.image_discovery_topic(image),
                self.comms_discovery.image_discovery_config(image),
                retain=True,
                timeout=2.0,
            )
        self._discovery_published = True
        log.info(
            "published comms HA discovery configs for %d primary + %d derived + %d image entities",
            len(ENTITIES) + 1, len(DERIVED_ENTITIES), len(IMAGE_ENTITIES),
        )
        self._seed_state_mirrors()

    def _seed_state_mirrors(self, lookback: int = 200) -> None:
        """Backfill the retained state-mirror topic for each comms HA entity
        with the most recent matching event from the source DBs.

        Without this, the HA entities show "unknown" until the next live
        event of each kind fires — which for low-volume entities like
        last_voicemail or last_facetime_call could be days or weeks.

        Scans up to ``lookback`` rows per source in DESCENDING order and
        keeps only the FIRST occurrence of each event_path. Publishes each
        directly to the state-mirror topic with retain=True, bypassing the
        outbox (this is one-shot at startup, the outbox would just defer
        the same publish).
        """
        if self.publisher is None or self.comms_discovery is None:
            return

        # Map every entity's event_path → entity for quick lookup.
        needed: dict[str, "object"] = {}
        for entity in ENTITIES:
            for event_path in entity.event_paths:
                needed.setdefault(event_path, entity)

        seeded: dict[str, str] = {}  # event_path -> "source:rowid" for log
        for source in self.sources:
            iter_recent = getattr(source, "iter_recent", None)
            if iter_recent is None:
                continue
            try:
                for event_path, payload in iter_recent(lookback):
                    if event_path in seeded or event_path not in needed:
                        continue
                    entity = needed[event_path]
                    # Strip binary photo bytes from the JSON payload and
                    # publish them separately to the matching image topic
                    # so HA's image entity has a value to render at the
                    # very first restart, before any new event has fired.
                    photo_bytes = payload.pop("__photo_bytes", None)
                    payload.pop("__photo_mime", None)
                    enriched = {"host": self._host_slug, **payload}
                    topic = self.comms_discovery.state_topic(entity)
                    self.publisher.publish_event_with_ack(
                        topic, enriched, retain=True, timeout=2.0
                    )
                    if photo_bytes is not None:
                        image_topic = self.comms_discovery.image_topic_for_parent(
                            entity.slug
                        )
                        if image_topic is not None:
                            self.publisher.publish_raw(
                                image_topic, photo_bytes, qos=1, retain=True
                            )
                    seeded[event_path] = (
                        f"{type(source).__name__}:"
                        f"{payload.get('rowid') or payload.get('pk') or '?'}"
                    )
                    if len(seeded) >= len(needed):
                        break
            except Exception:
                log.exception(
                    "error seeding state mirrors from %s", type(source).__name__
                )
            if len(seeded) >= len(needed):
                break

        if seeded:
            log.info(
                "seeded %d/%d HA state-mirror topic(s) from history: %s",
                len(seeded), len(needed), sorted(seeded.keys()),
            )
        else:
            log.info(
                "no historical events found within last %d rows to seed "
                "state mirrors", lookback,
            )

    # ---- outbound message send ----------------------------------------------

    def _outbound_send_topic(self) -> str:
        return self._comms_event_topic("messages/send")

    def _handle_send_request(self, _topic: str, payload: bytes) -> None:
        if self.outbound is None:
            return
        try:
            req = SendRequest.parse(payload)
        except Exception as e:  # noqa: BLE001
            log.warning("invalid send request: %s", e)
            self._emit(
                "messages/send_result",
                {"request_id": None, "success": False, "error": f"invalid request: {e}"},
            )
            return
        result = self.outbound.send(req)
        log.info("send to %s via %s: success=%s", req.to, req.service, result["success"])
        self._emit("messages/send_result", result)

    # ---- comms init-state ---------------------------------------------------

    def init_state(self) -> None:
        for source in self.sources:
            source.init_state(self.state)
        self.state.save()
        log.info("state initialized to current max IDs at %s", self.state.path)

    def run_once_dry(self) -> None:
        """Single-poll dry-run for diagnostics (no MQTT publish)."""
        for source in self.sources:
            for event_path, payload in source.poll(self.state):
                self._emit(event_path, payload)
        self.state.save()

    # ---- comms polling thread -----------------------------------------------

    def _comms_loop(self) -> None:
        log.info(
            "comms polling started (poll every %ds, sources=%s)",
            self.cfg.bridge.poll_interval_seconds,
            [type(s).__name__ for s in self.sources],
        )
        while not self._stop.is_set():
            try:
                for source in self.sources:
                    for event_path, payload in source.poll(self.state):
                        self._emit(event_path, payload)
                self.state.save()
                self._drain_outbox()
            except Exception:
                log.exception("error during comms poll cycle (continuing)")
            self._stop.wait(self.cfg.bridge.poll_interval_seconds)
        log.info("comms polling stopped")

    # ---- supervisor ---------------------------------------------------------

    async def _run_phase_loop(self, ticker: AbstractTicker) -> None:
        try:
            await ticker.publish_discovery(self.publisher)  # type: ignore[arg-type]
        except Exception:
            log.exception("ticker %s discovery publish failed; continuing", ticker.name)

        while True:
            try:
                await ticker.run_once(self.publisher)  # type: ignore[arg-type]
            except Exception:
                log.exception("ticker %s run_once failed", ticker.name)
            interval = ticker.interval_seconds
            if interval is None:
                # Self-paced (Phase C); yield briefly so we can be cancelled.
                await asyncio.sleep(0)
            else:
                await asyncio.sleep(interval)

    async def _run_async(self) -> None:
        assert self.publisher is not None

        # Subscribe + start MQTT before launching any work.
        if self.outbound is not None:
            self.publisher.subscribe(self._outbound_send_topic(), self._handle_send_request)
            log.info("subscribed to outbound send topic %s", self._outbound_send_topic())
        if self.controls_handler is not None:
            self.controls_handler.subscribe()
        self.publisher.start()
        if not self.publisher.wait_until_connected(timeout=10.0):
            log.warning("MQTT not connected after 10s; continuing — outbox will buffer")
        # Discovery has to wait until the broker is connected — paho's
        # ``publish`` is fire-and-forget when offline, so the discovery
        # configs would silently disappear into the queue.
        if self.controls_handler is not None and self.publisher.is_connected():
            self.controls_handler.publish_discovery()

        if self.realtime_calls is not None:
            self.realtime_calls.start()

        # Launch the comms polling thread.
        self._comms_thread = threading.Thread(
            target=self._comms_loop, name="comms-poll", daemon=True
        )
        self._comms_thread.start()

        # Build and supervise phase tickers.
        enabled = [t for t in self.tickers if t.enabled]
        if enabled:
            log.info(
                "starting phase supervisor with %d ticker(s): %s",
                len(enabled), [t.name for t in enabled],
            )
        else:
            log.info("no enabled phase tickers (comms-only mode)")

        loop = asyncio.get_running_loop()
        stop_event = asyncio.Event()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, stop_event.set)

        phase_tasks = [
            asyncio.create_task(self._run_phase_loop(t), name=t.name)
            for t in enabled
        ]
        stop_task = asyncio.create_task(stop_event.wait(), name="stop-wait")

        try:
            done, _pending = await asyncio.wait(
                [*phase_tasks, stop_task], return_when=asyncio.FIRST_COMPLETED
            )
            # If a phase ticker exited, log it (the rest keep running).
            for task in done:
                if task is stop_task:
                    continue
                exc = task.exception()
                if exc is not None:
                    log.error("ticker %s exited with %s", task.get_name(), exc)
        finally:
            self._stop.set()
            for t in phase_tasks:
                t.cancel()
            if phase_tasks:
                await asyncio.gather(*phase_tasks, return_exceptions=True)

            if self._comms_thread is not None:
                self._comms_thread.join(timeout=5.0)
            if self.realtime_calls is not None:
                self.realtime_calls.stop()
            if self.publisher is not None:
                self.publisher.stop()

    def run(self) -> int:
        if self.publisher is None:
            log.error("Bridge.run() called in dry-run mode; use run_once_dry() instead")
            return 1
        log.info(
            "host identity: slug=%s friendly=%r serial=%s en0_mac=%s",
            self._host_slug,
            self._host_friendly_name,
            self._serial_number,
            self._mac_address,
        )
        try:
            asyncio.run(self._run_async())
        except KeyboardInterrupt:
            pass
        log.info("macos-mqtt-bridge stopped")
        return 0


# Helper used by older tests (and dump-once) to iterate one source's events
# transiently without persisting state.
def iter_source_events(source):
    state = State("/dev/null")
    yield from source.poll(state)
