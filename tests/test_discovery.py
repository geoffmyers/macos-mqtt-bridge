"""Tests for the comms half's HA MQTT discovery layer."""

from __future__ import annotations

from pathlib import Path

from macos_bridge.config import load_config
from macos_bridge.discovery import ENTITIES, CommsHADiscovery


def _cfg():
    here = Path(__file__).resolve()
    return load_config(here.parents[1] / "config.example.yaml")


def _device_block(host: str) -> dict:
    return {
        "identifiers": [f"macos-mqtt-bridge:{host}"],
        "name": f"macOS Bridge — {host}",
        "model": "macos-mqtt-bridge",
    }


def test_entities_have_unique_slugs():
    slugs = [e.slug for e in ENTITIES]
    assert len(slugs) == len(set(slugs))


def test_every_messages_phone_facetime_voicemail_event_has_an_entity():
    expected = {
        "messages/received",
        "messages/sent",
        "messages/reaction",
        "messages/edited",
        "messages/retracted",
        "messages/group_event",
        "phone/ended",
        "phone/missed",
        "facetime/ended",
        "facetime/missed",
        "voicemail/received",
        "facetime/audio_message_received",
    }
    covered = {ep for e in ENTITIES for ep in e.event_paths}
    missing = expected - covered
    assert not missing, f"events without HA entities: {missing}"


def test_state_and_discovery_topics_are_well_formed():
    cfg = _cfg()
    d = CommsHADiscovery(cfg, "alex_macbookpro", _device_block("alex_macbookpro"))
    for e in ENTITIES:
        st = d.state_topic(e)
        dt = d.discovery_topic(e)
        # State mirror is flat under <prefix>/<host>/state/<slug>; no comms/ infix.
        assert st.startswith("macos/alex_macbookpro/state/")
        # Discovery unique_ids drop the legacy comms_ infix.
        assert dt.startswith("homeassistant/sensor/macos_alex_macbookpro_")
        assert "_comms_" not in dt
        assert dt.endswith("/config")


def test_event_topics_are_flat_under_host():
    cfg = _cfg()
    d = CommsHADiscovery(cfg, "alex_macbookpro", _device_block("alex_macbookpro"))
    assert d.event_topic("messages/sent") == "macos/alex_macbookpro/messages/sent"
    assert d.event_topic("phone/ended") == "macos/alex_macbookpro/phone/ended"
    assert d.event_topic("voicemail/received") == "macos/alex_macbookpro/voicemail/received"


def test_entity_for_dispatches_event_paths():
    cfg = _cfg()
    d = CommsHADiscovery(cfg, "host", _device_block("host"))
    assert d.entity_for("messages/received").slug == "last_message_received"
    assert d.entity_for("phone/missed").slug == "last_missed_phone_call"
    assert d.entity_for("voicemail/received").slug == "last_voicemail"
    assert d.entity_for("unknown/event") is None


def test_discovery_config_has_all_required_ha_fields():
    cfg = _cfg()
    d = CommsHADiscovery(cfg, "host", _device_block("host"))
    e = ENTITIES[0]
    config = d.discovery_config(e)
    for k in ("name", "unique_id", "state_topic", "value_template", "device"):
        assert k in config
    assert config["device"]["identifiers"] == ["macos-mqtt-bridge:host"]
    # Comms entities are HISTORICAL — they deliberately omit availability
    # so HA keeps showing the last retained payload even when the bridge
    # is offline. (See discovery.py::discovery_config docstring.)
    assert "availability_topic" not in config
    assert "payload_available" not in config
    assert "payload_not_available" not in config
    assert config["unique_id"].startswith("macos_host_last_")
    assert "_comms_" not in config["unique_id"]


def test_binary_sensor_online_config():
    cfg = _cfg()
    d = CommsHADiscovery(cfg, "host", _device_block("host"))
    topic, config = d.binary_sensor_online()
    assert topic == "homeassistant/binary_sensor/macos_host_online/config"
    assert config["device_class"] == "connectivity"
    assert config["state_topic"] == cfg.mqtt.lwt_topic
    assert config["payload_on"] == cfg.mqtt.lwt_online
    assert config["payload_off"] == cfg.mqtt.lwt_offline
