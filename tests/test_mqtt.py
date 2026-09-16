"""Tests for the merged MqttPublisher and discovery payload helpers."""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from macos_bridge.config import MqttConfig
from macos_bridge.mqtt import MqttPublisher, build_discovery_payload


def _make_config() -> MqttConfig:
    return MqttConfig(
        host="broker.lan",
        port=8883,
        tls=True,
        ca_file="/etc/ssl/cert.pem",
        username_env="MQTT_USERNAME",
        password_env="MQTT_PASSWORD",
        client_id="test-client",
        lwt_topic="bridge/test/status",
    )


@pytest.fixture(autouse=True)
def _stub_paho_client(monkeypatch: pytest.MonkeyPatch):
    """All tests in this module operate on a stubbed paho Client; they never
    touch the network."""
    fake_client = MagicMock()
    # paho.Client is constructed inside the toolkit's ThreadedPublisher
    # (parent class). Patch it there.
    monkeypatch.setattr(
        "ha_mqtt_bridge.paho_publisher.paho.Client",
        MagicMock(return_value=fake_client),
    )
    monkeypatch.setenv("MQTT_USERNAME", "u")
    monkeypatch.setenv("MQTT_PASSWORD", "p")
    return fake_client


def test_host_device_block_minimal(_stub_paho_client):
    pub = MqttPublisher(
        _make_config(),
        host_slug="alex_macbookpro",
        sw_version="0.1.0",
    )
    block = pub.host_device_block()
    assert block["identifiers"] == ["macos-mqtt-bridge:alex_macbookpro"]
    assert block["name"] == "macOS Bridge — alex_macbookpro"
    assert block["model"] == "macos-mqtt-bridge"
    assert block["sw_version"] == "0.1.0"
    assert "connections" not in block
    assert "serial_number" not in block


def test_host_device_block_with_serial_and_mac(_stub_paho_client):
    pub = MqttPublisher(
        _make_config(),
        host_slug="alex_macbookpro",
        sw_version="0.1.0",
        host_friendly_name="Alex's MacBook Pro",
        serial_number="EXAMPLE0001",
        mac_address="00:00:5e:00:53:00",
    )
    block = pub.host_device_block()
    assert "macos-mqtt-bridge:alex_macbookpro" in block["identifiers"]
    assert "apple-serial:EXAMPLE0001" in block["identifiers"]
    assert block["serial_number"] == "EXAMPLE0001"
    assert block["connections"] == [["mac", "00:00:5e:00:53:00"]]
    assert block["name"] == "macOS Bridge — Alex's MacBook Pro"


def test_build_discovery_payload_for_phase_a_total():
    device_block = {
        "identifiers": ["macos-mqtt-bridge:alex_macbookpro"],
        "name": "macOS Bridge — Alex's MacBook Pro",
    }
    payload = build_discovery_payload(
        name="Today's Total",
        unique_id="macos_alex_macbookpro_today_total",
        state_topic="macos/alex_macbookpro/today/total",
        availability_topic="macos/alex_macbookpro/status",
        device_class="duration",
        unit_of_measurement="h",
        state_class="measurement",
        icon="mdi:laptop",
        device=device_block,
    )
    assert payload["name"] == "Today's Total"
    assert payload["unique_id"] == "macos_alex_macbookpro_today_total"
    assert payload["device_class"] == "duration"
    assert payload["payload_available"] == "online"
    assert payload["payload_not_available"] == "offline"


def test_publisher_publishes_state(_stub_paho_client):
    pub = MqttPublisher(_make_config(), host_slug="test_host", sw_version="0.1.0")
    pub.publish_state("bridge/test/today/total", "42")
    _stub_paho_client.publish.assert_called_with(
        "bridge/test/today/total", "42", qos=0, retain=True
    )


def test_publisher_publishes_discovery_with_qos1(_stub_paho_client):
    pub = MqttPublisher(_make_config(), host_slug="test_host", sw_version="0.1.0")
    payload = {"name": "X", "unique_id": "x"}
    pub.publish_discovery(component="sensor", unique_id="x", payload=payload)
    expected_topic = "homeassistant/sensor/x/config"
    _stub_paho_client.publish.assert_any_call(
        expected_topic,
        json.dumps(payload, sort_keys=True, default=str),
        qos=1,
        retain=True,
    )


def test_publisher_sets_lwt_at_construct(_stub_paho_client):
    MqttPublisher(_make_config(), host_slug="test_host", sw_version="0.1.0")
    _stub_paho_client.will_set.assert_called_with(
        "bridge/test/status", payload="offline", qos=1, retain=True
    )


def test_publisher_publish_event_uses_event_qos_and_retain_default(
    _stub_paho_client,
):
    pub = MqttPublisher(_make_config(), host_slug="test_host", sw_version="0.1.0")
    pub.publish_event("foo/bar", {"a": 1})
    _stub_paho_client.publish.assert_called_with(
        "foo/bar", payload='{"a":1}', qos=1, retain=False
    )
