"""Tests for ha_mqtt_bridge.discovery."""

from __future__ import annotations

import pytest

from ha_mqtt_bridge.discovery import (
    KNOWN_COMPONENTS,
    availability_block,
    build_device_block,
    build_discovery_payload,
)


class TestBuildDeviceBlock:
    def test_minimal(self) -> None:
        block = build_device_block(identifiers=["abc"], name="My Device")
        assert block == {"identifiers": ["abc"], "name": "My Device"}

    def test_full(self) -> None:
        block = build_device_block(
            identifiers=["abc", "def"],
            name="My Device",
            manufacturer="Acme",
            model="M1",
            sw_version="1.2.3",
            hw_version="rev-A",
            serial_number="SN123",
            connections=[["mac", "aa:bb:cc:dd:ee:ff"]],
            via_device="parent",
            configuration_url="http://192.0.2.1/",
            suggested_area="Kitchen",
        )
        assert block == {
            "identifiers": ["abc", "def"],
            "name": "My Device",
            "manufacturer": "Acme",
            "model": "M1",
            "sw_version": "1.2.3",
            "hw_version": "rev-A",
            "serial_number": "SN123",
            "connections": [["mac", "aa:bb:cc:dd:ee:ff"]],
            "via_device": "parent",
            "configuration_url": "http://192.0.2.1/",
            "suggested_area": "Kitchen",
        }

    def test_none_optional_fields_are_omitted(self) -> None:
        block = build_device_block(
            identifiers=["abc"], name="X", manufacturer=None, model=None
        )
        assert "manufacturer" not in block
        assert "model" not in block

    def test_empty_connections_is_omitted(self) -> None:
        # A bridge may pass an empty list when no MAC is known — should drop
        # the field rather than ship `"connections": []`.
        block = build_device_block(identifiers=["abc"], name="X", connections=[])
        assert "connections" not in block

    def test_connections_are_copied(self) -> None:
        conn = [["mac", "aa:bb"]]
        block = build_device_block(identifiers=["abc"], name="X", connections=conn)
        block["connections"][0].append("mutation")
        # Original input not mutated.
        assert conn == [["mac", "aa:bb"]]

    def test_identifiers_are_copied(self) -> None:
        ids = ["abc"]
        block = build_device_block(identifiers=ids, name="X")
        block["identifiers"].append("def")
        assert ids == ["abc"]


class TestAvailabilityBlock:
    def test_defaults(self) -> None:
        assert availability_block("bridge/online") == {
            "availability_topic": "bridge/online",
            "payload_available": "online",
            "payload_not_available": "offline",
        }

    def test_overrides(self) -> None:
        assert availability_block(
            "x", payload_available="up", payload_not_available="down"
        ) == {
            "availability_topic": "x",
            "payload_available": "up",
            "payload_not_available": "down",
        }


class TestBuildDiscoveryPayload:
    DEVICE = {"identifiers": ["d1"], "name": "D"}

    def test_required_only(self) -> None:
        p = build_discovery_payload(
            name="Temp",
            unique_id="d1_temp",
            state_topic="x/d1/temp",
            device=self.DEVICE,
        )
        assert p == {
            "name": "Temp",
            "unique_id": "d1_temp",
            "state_topic": "x/d1/temp",
            "device": self.DEVICE,
        }

    def test_all_optionals_present(self) -> None:
        p = build_discovery_payload(
            name="Battery",
            unique_id="d1_bat",
            object_id="d1_battery",
            state_topic="x/d1/battery",
            device=self.DEVICE,
            device_class="battery",
            unit_of_measurement="%",
            state_class="measurement",
            icon="mdi:battery",
            entity_category="diagnostic",
            json_attributes_topic="x/d1/battery/attrs",
            value_template="{{ value }}",
            availability_topic="x/bridge/online",
            has_entity_name=False,
            enabled_by_default=True,
        )
        assert p["object_id"] == "d1_battery"
        assert p["device_class"] == "battery"
        assert p["unit_of_measurement"] == "%"
        assert p["state_class"] == "measurement"
        assert p["icon"] == "mdi:battery"
        assert p["entity_category"] == "diagnostic"
        assert p["json_attributes_topic"] == "x/d1/battery/attrs"
        assert p["value_template"] == "{{ value }}"
        assert p["has_entity_name"] is False
        assert p["enabled_by_default"] is True
        assert p["availability_topic"] == "x/bridge/online"
        assert p["payload_available"] == "online"
        assert p["payload_not_available"] == "offline"

    def test_binary_sensor_payload_on_off(self) -> None:
        p = build_discovery_payload(
            name="Leak",
            unique_id="d1_leak",
            state_topic="x/d1/leak",
            device=self.DEVICE,
            device_class="moisture",
            payload_on="ON",
            payload_off="OFF",
        )
        assert p["payload_on"] == "ON"
        assert p["payload_off"] == "OFF"
        # No availability fields sneak in just because device_class is set.
        assert "availability_topic" not in p

    def test_none_fields_are_omitted(self) -> None:
        p = build_discovery_payload(
            name="X",
            unique_id="u",
            state_topic="s",
            device=self.DEVICE,
            icon=None,
            entity_category=None,
        )
        assert "icon" not in p
        assert "entity_category" not in p

    def test_availability_overrides(self) -> None:
        p = build_discovery_payload(
            name="X",
            unique_id="u",
            state_topic="s",
            device=self.DEVICE,
            availability_topic="lwt",
            payload_available="UP",
            payload_not_available="DOWN",
        )
        assert p["payload_available"] == "UP"
        assert p["payload_not_available"] == "DOWN"

    def test_payload_available_without_topic_raises(self) -> None:
        with pytest.raises(ValueError, match="payload_available"):
            build_discovery_payload(
                name="X",
                unique_id="u",
                state_topic="s",
                device=self.DEVICE,
                payload_available="up",
            )

    def test_options_list_is_copied(self) -> None:
        opts = ["A", "B"]
        p = build_discovery_payload(
            name="X",
            unique_id="u",
            state_topic="s",
            device=self.DEVICE,
            options=opts,
        )
        p["options"].append("MUTATED")
        assert opts == ["A", "B"]

    def test_known_components_includes_basics(self) -> None:
        # Sanity check: the documented components the bridges use are
        # all present.
        for c in ("sensor", "binary_sensor", "button", "switch",
                  "number", "select", "text", "image", "device_tracker"):
            assert c in KNOWN_COMPONENTS

    def test_component_argument_does_not_leak_into_payload(self) -> None:
        p = build_discovery_payload(
            name="X",
            unique_id="u",
            state_topic="s",
            device=self.DEVICE,
            component="sensor",
        )
        assert "component" not in p
