"""Home Assistant MQTT Discovery payload + device-block builders.

Pure functions only. No MQTT client, no JSON serialization, no I/O. The
caller serializes the returned dict via ``json.dumps`` and publishes it
to the appropriate ``<discovery_prefix>/<component>/<unique_id>/config``
topic with ``retain=True``.

Field semantics follow https://www.home-assistant.io/integrations/mqtt/#mqtt-discovery
and the supplementary device-class / state-class enums documented at each
component's own integration page (sensor, binary_sensor, image, etc.).

The builders intentionally accept only a curated subset of HA Discovery's
~100+ optional fields — the union of what the bridges actually use today. New fields can be added as they become necessary;
keep them keyword-only and Optional with sensible defaults.
"""

from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger(__name__)

# Reserved component types. HA accepts more (cover, light, fan, etc.) but
# only these are referenced by the existing bridges. Used purely as a
# documentation/validation hint; not enforced by build_discovery_payload
# so a caller can pass any value HA accepts.
KNOWN_COMPONENTS = frozenset(
    {
        "binary_sensor",
        "button",
        "device_tracker",
        "image",
        "number",
        "select",
        "sensor",
        "switch",
        "text",
    }
)


def build_device_block(
    *,
    identifiers: list[str],
    name: str,
    manufacturer: str | None = None,
    model: str | None = None,
    sw_version: str | None = None,
    serial_number: str | None = None,
    connections: list[list[str]] | None = None,
    via_device: str | None = None,
    configuration_url: str | None = None,
    hw_version: str | None = None,
    suggested_area: str | None = None,
) -> dict[str, Any]:
    """Build an HA ``device`` block for inclusion in discovery payloads.

    ``identifiers`` is the canonical list that HA dedupes against; the
    first entry is the one HA renders. ``connections`` is a list of
    ``[type, value]`` pairs, e.g. ``[["mac", "aa:bb:cc:dd:ee:ff"]]``,
    which HA uses to merge with other integrations that already know the
    device by hardware identity.

    ``via_device`` points at another device's first identifier — HA
    renders the resulting hierarchy as a parent/child relationship on
    the Devices page (a sensor under its gateway, say, or a sub-device
    under its parent).
    """
    block: dict[str, Any] = {
        "identifiers": list(identifiers),
        "name": name,
    }
    if manufacturer is not None:
        block["manufacturer"] = manufacturer
    if model is not None:
        block["model"] = model
    if sw_version is not None:
        block["sw_version"] = sw_version
    if hw_version is not None:
        block["hw_version"] = hw_version
    if serial_number is not None:
        block["serial_number"] = serial_number
    if connections:
        block["connections"] = [list(pair) for pair in connections]
    if via_device is not None:
        block["via_device"] = via_device
    if configuration_url is not None:
        block["configuration_url"] = configuration_url
    if suggested_area is not None:
        block["suggested_area"] = suggested_area
    return block


def availability_block(
    lwt_topic: str,
    *,
    payload_available: str = "online",
    payload_not_available: str = "offline",
) -> dict[str, Any]:
    """Return the ``availability_topic`` + payload pair as discovery-ready
    keyword args.

    Use ``**availability_block(...)`` inside ``build_discovery_payload``
    or splat into an inline dict. Pass ``payload_available`` /
    ``payload_not_available`` only when overriding the conventional
    ``"online"`` / ``"offline"`` wire strings.

    Returns a dict so callers can splat with ``**`` — kept separate from
    ``build_discovery_payload`` so phase-specific code can choose whether
    a given entity should track availability at all (historical sensors
    omit it so HA keeps showing the last retained value when the bridge
    is offline).
    """
    return {
        "availability_topic": lwt_topic,
        "payload_available": payload_available,
        "payload_not_available": payload_not_available,
    }


def build_discovery_payload(
    *,
    name: str,
    unique_id: str,
    state_topic: str,
    device: dict[str, Any],
    object_id: str | None = None,
    component: str | None = None,
    device_class: str | None = None,
    state_class: str | None = None,
    unit_of_measurement: str | None = None,
    icon: str | None = None,
    entity_category: str | None = None,
    enabled_by_default: bool | None = None,
    json_attributes_topic: str | None = None,
    value_template: str | None = None,
    payload_on: str | None = None,
    payload_off: str | None = None,
    options: list[str] | None = None,
    availability_topic: str | None = None,
    payload_available: str | None = None,
    payload_not_available: str | None = None,
    has_entity_name: bool | None = None,
    expire_after: int | None = None,
    force_update: bool | None = None,
) -> dict[str, Any]:
    """Build an HA MQTT Discovery config payload.

    Required fields (per HA):
        ``name``, ``unique_id``, ``state_topic``, ``device``.

    Common optional fields are flattened into keyword arguments. Any
    field passed as ``None`` is omitted from the result, so the payload
    on the wire contains only the keys the caller explicitly chose.

    Availability semantics:
        Pass ``availability_topic`` to make HA flip the entity to
        "Unavailable" when the bridge's LWT fires. Omit it for
        historical / point-in-time entities that should keep displaying
        their last retained value when the bridge is offline. The
        ``payload_available`` / ``payload_not_available`` defaults
        (``"online"`` / ``"offline"``) are applied automatically when
        ``availability_topic`` is set and they aren't explicitly
        overridden.

    The ``component`` argument is informational — HA Discovery encodes
    the component in the *topic*, not the payload — but having it on
    the dataclass-like return makes it convenient for callers building
    the discovery topic separately. It is NOT written into the returned
    dict.
    """
    payload: dict[str, Any] = {
        "name": name,
        "unique_id": unique_id,
        "state_topic": state_topic,
        "device": device,
    }
    if object_id is not None:
        payload["object_id"] = object_id
    if device_class is not None:
        payload["device_class"] = device_class
    if state_class is not None:
        payload["state_class"] = state_class
    if unit_of_measurement is not None:
        payload["unit_of_measurement"] = unit_of_measurement
    if icon is not None:
        payload["icon"] = icon
    if entity_category is not None:
        payload["entity_category"] = entity_category
    if enabled_by_default is not None:
        payload["enabled_by_default"] = enabled_by_default
    if json_attributes_topic is not None:
        payload["json_attributes_topic"] = json_attributes_topic
    if value_template is not None:
        payload["value_template"] = value_template
    if payload_on is not None:
        payload["payload_on"] = payload_on
    if payload_off is not None:
        payload["payload_off"] = payload_off
    if options is not None:
        payload["options"] = list(options)
    if has_entity_name is not None:
        payload["has_entity_name"] = has_entity_name
    if expire_after is not None:
        payload["expire_after"] = expire_after
    if force_update is not None:
        payload["force_update"] = force_update

    if availability_topic is not None:
        payload["availability_topic"] = availability_topic
        payload["payload_available"] = (
            payload_available if payload_available is not None else "online"
        )
        payload["payload_not_available"] = (
            payload_not_available if payload_not_available is not None else "offline"
        )
    elif payload_available is not None or payload_not_available is not None:
        # Defensive: an explicit payload_available without availability_topic
        # is almost always a caller mistake. Surface it as a clear error
        # rather than silently dropping the field.
        raise ValueError(
            "payload_available / payload_not_available passed without "
            "availability_topic — set availability_topic or omit both"
        )

    # Reference component validation for caller convenience. Skipped if
    # None so callers that don't carry component info aren't penalized.
    if component is not None and component not in KNOWN_COMPONENTS:
        # Not an error — HA supports many more components than this list
        # (cover, light, fan, climate, ...) — the toolkit just doesn't
        # have curated test coverage for them yet. Logged at debug so a
        # bridge author who *meant* a known component and mistyped it
        # (or a stray unique_id crept into the component slot) has
        # something to grep for without every legitimate-but-uncovered
        # component spamming production logs at warning level.
        log.debug(
            "discovery component %r not in KNOWN_COMPONENTS "
            "(informational only — HA accepts more than the toolkit "
            "has test coverage for)",
            component,
        )

    return payload
