"""Home Assistant MQTT discovery for the comms half of the bridge.

Each entity here is a sensor backed by a retained state-mirror topic;
the bridge updates that state topic every time the matching event fires.

Topic layout (flat — no `comms/` infix):
  Event  (fire-and-forget):     <prefix>/<host>/<event_path>     (e.g. messages/sent)
  State  (retained mirror):     <prefix>/<host>/state/<slug>
  Discovery (retained, once):   <discovery_prefix>/sensor/<unique_id>/config

The state topic and the event topic carry the same payload — state is just
"the last one, retained" so HA shows the most recent value after a restart.

The phase tickers register their own discovery configs separately
(``phases/<scope>.publish_discovery``); this module covers ONLY the
comms event entities.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from macos_bridge.config import Config

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Entity:
    slug: str  # used in state topic + unique_id
    name: str
    icon: str
    event_paths: tuple[str, ...]
    value_template: str
    component: str = "sensor"


@dataclass(frozen=True)
class ImageEntity:
    """An HA MQTT image entity that surfaces the contact photo from the
    most recent matching event. Bytes are published to ``image_topic``;
    HA renders them as the entity's current image. Replaces the old
    base64-data-URI-in-JSON approach which bloated event payloads to
    100+ KB and broke value_template-derived sensors.

    Topic: ``<prefix>/<host>/images/<slug>`` — raw bytes, retain=True so
    HA can re-fetch on restart and an absent-bridge HA still shows the
    last known photo.

    Discovery: ``<discovery_prefix>/image/<unique_id>/config`` per HA's
    MQTT image integration (https://www.home-assistant.io/integrations/image.mqtt/).
    """

    slug: str       # unique_id suffix; image topic suffix
    name: str       # HA-visible entity name
    icon: str
    parent_slug: str  # which Entity's events trigger image republish


@dataclass(frozen=True)
class DerivedEntity:
    """A read-only HA sensor that points at an existing primary entity's
    retained state-mirror topic with a different ``value_template``.

    Used to surface specific fields from an event payload as their own HA
    entities (e.g. ``last_message_received.text`` becomes a string sensor;
    ``last_phone_call.duration_seconds`` becomes a duration sensor).

    The bridge does NOT publish anything to a derived entity's own topic;
    its state is whatever ``parent_slug``'s state-mirror payload yields
    when run through ``value_template``. This means derived entities
    automatically stay in sync — when the parent updates, every derived
    sensor reading from it updates too.
    """

    slug: str  # HA unique_id suffix (macos_<host>_<slug>)
    name: str
    icon: str
    parent_slug: str  # the primary entity's state-topic slug to read from
    value_template: str
    device_class: str | None = None
    unit_of_measurement: str | None = None
    state_class: str | None = None
    component: str = "sensor"


# All HA entities exposed by the comms half. Each maps one or more event
# paths to a single state topic + sensor.
ENTITIES: tuple[Entity, ...] = (
    Entity(
        slug="last_message_received",
        name="Messages - Last Received",
        icon="mdi:message-text",
        event_paths=("messages/received",),
        value_template=(
            "{{ value_json.contact.full_name "
            "if value_json.contact is defined "
            "else value_json.handle }}"
        ),
    ),
    Entity(
        slug="last_message_sent",
        name="Messages - Last Sent",
        icon="mdi:message-arrow-right",
        event_paths=("messages/sent",),
        value_template=(
            "{{ value_json.contact.full_name "
            "if value_json.contact is defined "
            "else value_json.handle }}"
        ),
    ),
    Entity(
        slug="last_reaction",
        name="Messages - Last Reaction",
        icon="mdi:emoticon-outline",
        event_paths=("messages/reaction",),
        value_template=(
            "{{ value_json.reaction_kind }}"
            "{{ ' (Removed)' if value_json.is_remove else '' }}"
        ),
    ),
    Entity(
        slug="last_edited_message",
        name="Messages - Last Edited",
        icon="mdi:pencil",
        event_paths=("messages/edited",),
        value_template=(
            "{{ value_json.contact.full_name "
            "if value_json.contact is defined "
            "else value_json.handle }}"
        ),
    ),
    Entity(
        slug="last_retracted_message",
        name="Messages - Last Retracted",
        icon="mdi:undo",
        event_paths=("messages/retracted",),
        value_template=(
            "{{ value_json.contact.full_name "
            "if value_json.contact is defined "
            "else value_json.handle }}"
        ),
    ),
    Entity(
        slug="last_group_event",
        name="Messages - Last Group Event",
        icon="mdi:account-group",
        event_paths=("messages/group_event",),
        value_template="item_type={{ value_json.item_type }}",
    ),
    Entity(
        slug="last_phone_call",
        name="Phone - Last Call",
        icon="mdi:phone",
        event_paths=("phone/ended",),
        value_template=(
            "{{ value_json.contact.full_name "
            "if value_json.contact is defined "
            "else value_json.address }}"
        ),
    ),
    Entity(
        slug="last_missed_phone_call",
        name="Phone - Last Missed Call",
        icon="mdi:phone-missed",
        event_paths=("phone/missed",),
        value_template=(
            "{{ value_json.contact.full_name "
            "if value_json.contact is defined "
            "else value_json.address }}"
        ),
    ),
    Entity(
        slug="last_facetime_call",
        name="FaceTime - Last Call",
        icon="mdi:video",
        event_paths=("facetime/ended",),
        value_template=(
            "{{ value_json.contact.full_name "
            "if value_json.contact is defined "
            "else value_json.address }}"
        ),
    ),
    Entity(
        slug="last_missed_facetime_call",
        name="FaceTime - Last Missed Call",
        icon="mdi:video-off",
        event_paths=("facetime/missed",),
        value_template=(
            "{{ value_json.contact.full_name "
            "if value_json.contact is defined "
            "else value_json.address }}"
        ),
    ),
    Entity(
        slug="last_voicemail",
        name="Voicemail - Last",
        icon="mdi:voicemail",
        event_paths=("voicemail/received",),
        value_template=(
            "{{ value_json.contact.full_name "
            "if value_json.contact is defined "
            "else value_json.sender }}"
        ),
    ),
    Entity(
        slug="last_facetime_audio_message",
        name="FaceTime - Last Audio Message",
        icon="mdi:microphone-message",
        event_paths=("facetime/audio_message_received",),
        value_template=(
            "{{ value_json.contact.full_name "
            "if value_json.contact is defined "
            "else value_json.sender }}"
        ),
    ),
)


# Read-only derived sensors that surface specific fields from existing
# state-mirror payloads as their own HA entities. Each one points at a
# parent entity's state_topic via parent_slug; HA renders the parent's
# retained payload through this entity's value_template.
DERIVED_ENTITIES: tuple[DerivedEntity, ...] = (
    # ---- last message sent ----
    DerivedEntity(
        slug="last_message_sent_at",
        name="Messages - Last Sent At",
        icon="mdi:clock-outline",
        parent_slug="last_message_sent",
        value_template="{{ value_json.timestamp }}",
        device_class="timestamp",
    ),
    DerivedEntity(
        slug="last_message_sent_text",
        name="Messages - Last Sent Text",
        icon="mdi:message-text-outline",
        parent_slug="last_message_sent",
        # `or '(no text)'` keeps HA off "unknown" when the most recent
        # sent message was an attachment-only / sticker / tapback row.
        # Truncate to 252 chars + ellipsis: HA 2024.1+ rejects sensor
        # state values longer than 255 characters and shows them as
        # "Unknown". The full untruncated text is still available on
        # the parent state-mirror topic (last_message_sent.text) for
        # automations / templates that need it.
        value_template=(
            "{{ ((value_json.text or '(no text)') | string)[:252] + "
            "('...' if (value_json.text or '') | length > 252 else '') }}"
        ),
    ),
    # ---- last message received ----
    DerivedEntity(
        slug="last_message_received_at",
        name="Messages - Last Received At",
        icon="mdi:clock-outline",
        parent_slug="last_message_received",
        value_template="{{ value_json.timestamp }}",
        device_class="timestamp",
    ),
    DerivedEntity(
        slug="last_message_received_text",
        name="Messages - Last Received Text",
        icon="mdi:message-text-outline",
        parent_slug="last_message_received",
        # See last_message_sent_text for the 255-char rationale.
        value_template=(
            "{{ ((value_json.text or '(no text)') | string)[:252] + "
            "('...' if (value_json.text or '') | length > 252 else '') }}"
        ),
    ),
    # ---- last voicemail (phone) ----
    DerivedEntity(
        slug="last_voicemail_at",
        name="Voicemail - Last At",
        icon="mdi:clock-outline",
        parent_slug="last_voicemail",
        value_template="{{ value_json.timestamp }}",
        device_class="timestamp",
    ),
    DerivedEntity(
        slug="last_voicemail_transcript",
        name="Voicemail - Last Transcript",
        icon="mdi:text-recognition",
        parent_slug="last_voicemail",
        # See last_message_sent_text for the 255-char rationale.
        value_template=(
            "{{ ((value_json.transcription or '(no transcript)') | string)[:252] + "
            "('...' if (value_json.transcription or '') | length > 252 else '') }}"
        ),
    ),
    DerivedEntity(
        slug="last_voicemail_duration",
        name="Voicemail - Last Duration",
        icon="mdi:timer-outline",
        parent_slug="last_voicemail",
        value_template="{{ value_json.duration_seconds | float(0) | round(0) | int }}",
        device_class="duration",
        unit_of_measurement="s",
        state_class="measurement",
    ),
    # ---- last phone call ----
    DerivedEntity(
        slug="last_phone_call_started_at",
        name="Phone - Last Call Started At",
        icon="mdi:phone-outgoing",
        parent_slug="last_phone_call",
        value_template="{{ value_json.started_at }}",
        device_class="timestamp",
    ),
    DerivedEntity(
        slug="last_phone_call_ended_at",
        name="Phone - Last Call Ended At",
        icon="mdi:phone-hangup",
        parent_slug="last_phone_call",
        value_template="{{ value_json.ended_at }}",
        device_class="timestamp",
    ),
    DerivedEntity(
        slug="last_phone_call_duration",
        name="Phone - Last Call Duration",
        icon="mdi:timer-outline",
        parent_slug="last_phone_call",
        # Round to int seconds so HA renders cleanly as a duration.
        value_template="{{ value_json.duration_seconds | float(0) | round(0) | int }}",
        device_class="duration",
        unit_of_measurement="s",
        state_class="measurement",
    ),
    DerivedEntity(
        slug="last_phone_call_direction",
        name="Phone - Last Call Direction",
        icon="mdi:swap-horizontal",
        parent_slug="last_phone_call",
        value_template="{{ value_json.direction }}",
    ),
    # ---- last FaceTime call ----
    # Same payload shape as last_phone_call (both flow through the calls
    # source's _row_to_events), so identical value_templates apply.
    DerivedEntity(
        slug="last_facetime_call_started_at",
        name="FaceTime - Last Call Started At",
        icon="mdi:phone-outgoing",
        parent_slug="last_facetime_call",
        value_template="{{ value_json.started_at }}",
        device_class="timestamp",
    ),
    DerivedEntity(
        slug="last_facetime_call_ended_at",
        name="FaceTime - Last Call Ended At",
        icon="mdi:phone-hangup",
        parent_slug="last_facetime_call",
        value_template="{{ value_json.ended_at }}",
        device_class="timestamp",
    ),
    DerivedEntity(
        slug="last_facetime_call_duration",
        name="FaceTime - Last Call Duration",
        icon="mdi:timer-outline",
        parent_slug="last_facetime_call",
        value_template="{{ value_json.duration_seconds | float(0) | round(0) | int }}",
        device_class="duration",
        unit_of_measurement="s",
        state_class="measurement",
    ),
    DerivedEntity(
        slug="last_facetime_call_direction",
        name="FaceTime - Last Call Direction",
        icon="mdi:swap-horizontal",
        parent_slug="last_facetime_call",
        value_template="{{ value_json.direction }}",
    ),
)


# ---- HA Image entities (contact photos) ------------------------------------
#
# Every comms event that resolves a contact gets a paired image entity. The
# bridge publishes the contact's PNG/JPEG bytes (decoded from AddressBook's
# ZTHUMBNAILIMAGEDATA / ZIMAGEDATA blobs) to the image topic any time the
# parent event fires. Entities WITHOUT a contact (e.g. group_event) get no
# image entity.
IMAGE_ENTITIES: tuple[ImageEntity, ...] = (
    ImageEntity(
        slug="last_message_received_contact_photo",
        name="Messages - Last Received Contact Photo",
        icon="mdi:account-circle",
        parent_slug="last_message_received",
    ),
    ImageEntity(
        slug="last_message_sent_contact_photo",
        name="Messages - Last Sent Contact Photo",
        icon="mdi:account-circle",
        parent_slug="last_message_sent",
    ),
    ImageEntity(
        slug="last_reaction_contact_photo",
        name="Messages - Last Reaction Contact Photo",
        icon="mdi:account-circle",
        parent_slug="last_reaction",
    ),
    ImageEntity(
        slug="last_edited_message_contact_photo",
        name="Messages - Last Edited Contact Photo",
        icon="mdi:account-circle",
        parent_slug="last_edited_message",
    ),
    ImageEntity(
        slug="last_retracted_message_contact_photo",
        name="Messages - Last Retracted Contact Photo",
        icon="mdi:account-circle",
        parent_slug="last_retracted_message",
    ),
    ImageEntity(
        slug="last_phone_call_contact_photo",
        name="Phone - Last Call Contact Photo",
        icon="mdi:account-circle",
        parent_slug="last_phone_call",
    ),
    ImageEntity(
        slug="last_facetime_call_contact_photo",
        name="FaceTime - Last Call Contact Photo",
        icon="mdi:account-circle",
        parent_slug="last_facetime_call",
    ),
    ImageEntity(
        slug="last_voicemail_contact_photo",
        name="Voicemail - Last Contact Photo",
        icon="mdi:account-circle",
        parent_slug="last_voicemail",
    ),
)

# Reverse lookup: parent_slug → ImageEntity. Used by the runtime to find
# the right image topic when emitting an event.
IMAGE_ENTITY_BY_PARENT: dict[str, ImageEntity] = {
    ie.parent_slug: ie for ie in IMAGE_ENTITIES
}


@dataclass
class CommsHADiscovery:
    """Builds discovery configs and topic strings for the comms entities.

    Topic scheme (flat — no `comms/` infix):
      event:     <prefix>/<host>/<event_path>          (e.g. messages/sent, phone/ended)
      state:     <prefix>/<host>/state/<slug>          (HA state mirror, retained)
      discovery: <discovery_prefix>/sensor/macos_<host>_<slug>/config
    """

    cfg: Config
    hostname: str
    device_block: dict[str, Any]
    _by_event: dict[str, Entity] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self._by_event = {ep: e for e in ENTITIES for ep in e.event_paths}

    def _host_prefix(self) -> str:
        return f"{self.cfg.mqtt.topic_prefix}/{self.hostname}"

    def event_topic(self, event_path: str) -> str:
        return f"{self._host_prefix()}/{event_path}"

    def state_topic(self, entity: Entity) -> str:
        return f"{self._host_prefix()}/state/{entity.slug}"

    def discovery_topic(self, entity: Entity) -> str:
        unique = self._unique_id(entity)
        return f"{self.cfg.mqtt.discovery_prefix}/{entity.component}/{unique}/config"

    def entity_for(self, event_path: str) -> Entity | None:
        return self._by_event.get(event_path)

    def _unique_id(self, entity: Entity) -> str:
        return f"macos_{self.hostname}_{entity.slug}"

    def discovery_config(self, entity: Entity) -> dict:
        # Comms entities are HISTORICAL: their state is "the most recent X
        # event to ever happen on this Mac." When the bridge goes offline,
        # the last value is still meaningful, so we deliberately do NOT
        # tie them to the bridge's LWT — HA shows the retained payload
        # forever (or until a newer event overwrites it).
        unique = self._unique_id(entity)
        return {
            "name": entity.name,
            "unique_id": unique,
            "object_id": unique,
            "state_topic": self.state_topic(entity),
            "value_template": entity.value_template,
            "json_attributes_topic": self.state_topic(entity),
            "icon": entity.icon,
            "device": self.device_block,
        }

    # ---- derived entities (read existing parent state-mirror topics) ----

    def derived_unique_id(self, derived: DerivedEntity) -> str:
        return f"macos_{self.hostname}_{derived.slug}"

    def derived_state_topic(self, derived: DerivedEntity) -> str:
        """Derived entities re-use their parent's state-mirror topic; HA pulls
        the value out of the same retained payload via value_template."""
        return f"{self._host_prefix()}/state/{derived.parent_slug}"

    def derived_discovery_topic(self, derived: DerivedEntity) -> str:
        unique = self.derived_unique_id(derived)
        return f"{self.cfg.mqtt.discovery_prefix}/{derived.component}/{unique}/config"

    def derived_discovery_config(self, derived: DerivedEntity) -> dict:
        # Same reasoning as ``discovery_config``: derived entities read
        # historical state-mirror payloads, so don't gate them on bridge
        # availability.
        unique = self.derived_unique_id(derived)
        config: dict[str, Any] = {
            "name": derived.name,
            "unique_id": unique,
            "object_id": unique,
            "state_topic": self.derived_state_topic(derived),
            "value_template": derived.value_template,
            "icon": derived.icon,
            "device": self.device_block,
        }
        if derived.device_class is not None:
            config["device_class"] = derived.device_class
        if derived.unit_of_measurement is not None:
            config["unit_of_measurement"] = derived.unit_of_measurement
        if derived.state_class is not None:
            config["state_class"] = derived.state_class
        return config

    # ---- image entities (contact photos via HA MQTT image integration) ----

    def image_unique_id(self, image: ImageEntity) -> str:
        return f"macos_{self.hostname}_{image.slug}"

    def image_topic(self, image: ImageEntity) -> str:
        """Topic where the bridge publishes raw image bytes (PNG/JPEG)."""
        return f"{self._host_prefix()}/images/{image.slug}"

    def image_topic_for_parent(self, parent_slug: str) -> str | None:
        """Quick lookup for the runtime: given a state-mirror entity's
        slug, return the topic to publish its contact photo bytes to.
        Returns None if no image entity is registered for that parent."""
        image = IMAGE_ENTITY_BY_PARENT.get(parent_slug)
        return self.image_topic(image) if image else None

    def image_discovery_topic(self, image: ImageEntity) -> str:
        unique = self.image_unique_id(image)
        return f"{self.cfg.mqtt.discovery_prefix}/image/{unique}/config"

    def image_discovery_config(self, image: ImageEntity) -> dict:
        # Like the comms entities themselves, image entities are
        # historical: HA should keep showing the last known photo even
        # while the bridge is offline. So no availability_topic.
        # content_type defaults to image/png — the dominant format in
        # the macOS AddressBook pipeline. JPEG photos still render fine
        # because browsers detect MIME from file magic, not the header.
        unique = self.image_unique_id(image)
        return {
            "name": image.name,
            "unique_id": unique,
            "object_id": unique,
            "image_topic": self.image_topic(image),
            "content_type": "image/png",
            "icon": image.icon,
            "device": self.device_block,
        }

    def binary_sensor_online(self) -> tuple[str, dict]:
        # The "Online" sensor IS the bridge's liveness signal — useful but
        # not a top-level user-facing entity. Tagged diagnostic so HA
        # tucks it under the device's diagnostic section.
        unique = f"macos_{self.hostname}_online"
        topic = (
            f"{self.cfg.mqtt.discovery_prefix}/binary_sensor/{unique}/config"
        )
        config = {
            "name": "Online",
            "unique_id": unique,
            "object_id": unique,
            "state_topic": self.cfg.mqtt.lwt_topic,
            "payload_on": self.cfg.mqtt.lwt_online,
            "payload_off": self.cfg.mqtt.lwt_offline,
            "device_class": "connectivity",
            "entity_category": "diagnostic",
            "device": self.device_block,
        }
        return topic, config
