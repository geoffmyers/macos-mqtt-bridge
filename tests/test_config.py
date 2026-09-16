"""Tests for the merged macos_bridge.config schema and loader."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from macos_bridge.config import (
    Config,
    load_config,
    load_config_with_credentials,
)


def _minimal_dict():
    return {
        "bridge": {"hostname": "test_mac"},
        "mqtt": {
            "host": "broker.lan",
            "port": 8883,
            "tls": True,
            "ca_file": "/etc/ssl/cert.pem",
            "username_env": "MQTT_USERNAME",
            "password_env": "MQTT_PASSWORD",
        },
    }


def test_minimal_config_validates():
    cfg = Config.model_validate(_minimal_dict())
    assert cfg.bridge.hostname == "test_mac"
    assert cfg.mqtt.port == 8883
    assert cfg.aggregates.enabled is True  # phase_a defaults on


def test_tls_requires_ca_file():
    data = _minimal_dict()
    data["mqtt"]["ca_file"] = None
    with pytest.raises(ValidationError, match="ca_file"):
        Config.model_validate(data)


def test_phase_a_interval_must_be_positive():
    data = _minimal_dict()
    data["aggregates"] = {"enabled": True, "interval_seconds": 0}
    with pytest.raises(ValidationError):
        Config.model_validate(data)


def test_unknown_phase_field_rejected():
    data = _minimal_dict()
    data["aggregates"] = {"enabled": True, "interval_seconds": 900, "unknown_key": 42}
    with pytest.raises(ValidationError):
        Config.model_validate(data)


def test_default_log_level_is_info():
    cfg = Config.model_validate(_minimal_dict())
    assert cfg.bridge.log_level == "info"


def test_phases_default_disabled_except_a():
    cfg = Config.model_validate(_minimal_dict())
    assert cfg.aggregates.enabled is True   # daily aggregates always on
    assert cfg.per_app.enabled is False
    assert cfg.focused_app.enabled is False
    assert cfg.family.enabled is False
    assert cfg.activity.enabled is False
    assert cfg.system_state.enabled is False
    assert cfg.host_metrics.enabled is False


def test_phase_b_apps_parsed():
    data = _minimal_dict()
    data["per_app"] = {
        "enabled": True,
        "apps": [
            {"bundle_id": "com.apple.Safari", "friendly_name": "Safari"},
            {"bundle_id": "com.apple.logic10"},
        ],
    }
    cfg = Config.model_validate(data)
    assert cfg.per_app.enabled is True
    assert len(cfg.per_app.apps) == 2
    assert cfg.per_app.apps[0].bundle_id == "com.apple.Safari"
    assert cfg.per_app.apps[1].friendly_name is None


def test_phase_d_enabled_requires_organizer():
    data = _minimal_dict()
    data["family"] = {
        "enabled": True,
        "is_family_organizer": False,
        "members": [{"dsid": 111, "slug": "k1"}],
    }
    with pytest.raises(ValidationError, match="is_family_organizer"):
        Config.model_validate(data)


def test_phase_d_enabled_with_empty_members_is_valid():
    data = _minimal_dict()
    data["family"] = {
        "enabled": True,
        "is_family_organizer": True,
        "members": [],
    }
    cfg = Config.model_validate(data)
    assert cfg.family.enabled is True
    assert cfg.family.members == []


def test_default_topic_prefix_and_lwt():
    cfg = Config.model_validate(_minimal_dict())
    assert cfg.mqtt.topic_prefix == "macos"
    assert cfg.mqtt.lwt_topic == "macos/status"
    assert cfg.mqtt.discovery_prefix == "homeassistant"


def test_comms_event_sources_default_enabled():
    cfg = Config.model_validate(_minimal_dict())
    assert cfg.sources.messages.enabled is True
    assert cfg.sources.calls.enabled is True
    assert cfg.sources.voicemail.enabled is True


def test_load_config_expands_env_in_yaml(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("DARWIN_USER_DIR", "/private/var/folders/xx/yy/0")
    monkeypatch.setenv("HOME", str(tmp_path))

    yaml_path = tmp_path / "config.yaml"
    yaml_path.write_text("""
bridge:
  hostname: test_mac
mqtt:
  host: broker.lan
  port: 8883
  tls: true
  ca_file: /etc/ssl/cert.pem
phase_sources:
  knowledge_db: ~/Library/Application Support/Knowledge/knowledgeC.db
  rm_admin_local: ${DARWIN_USER_DIR}/com.apple.ScreenTimeAgent/Store/Local.sqlite
  rm_admin_cloud: ${DARWIN_USER_DIR}/com.apple.ScreenTimeAgent/Store/Cloud.sqlite
""")
    cfg = load_config(yaml_path)
    assert cfg.bridge.hostname == "test_mac"
    paths = cfg.expanded_paths()
    assert paths["knowledge_db"] == str(
        tmp_path / "Library/Application Support/Knowledge/knowledgeC.db"
    )
    assert paths["rm_admin_local"].startswith("/private/var/folders/xx/yy/0/")


def test_load_config_with_credentials_reads_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("MQTT_USERNAME", "u")
    monkeypatch.setenv("MQTT_PASSWORD", "p")

    yaml_path = tmp_path / "config.yaml"
    yaml_path.write_text("""
bridge:
  hostname: test_mac
mqtt:
  host: broker.lan
  port: 8883
  tls: true
  ca_file: /etc/ssl/cert.pem
""")
    cfg, creds = load_config_with_credentials(yaml_path)
    assert creds.username == "u"
    assert creds.password == "p"
    assert cfg.mqtt.host == "broker.lan"


def test_load_config_with_credentials_missing_env_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.delenv("MQTT_PASSWORD", raising=False)
    monkeypatch.setenv("MQTT_USERNAME", "u")

    yaml_path = tmp_path / "config.yaml"
    yaml_path.write_text("""
bridge:
  hostname: test_mac
mqtt:
  host: broker.lan
  port: 8883
  tls: true
  ca_file: /etc/ssl/cert.pem
""")
    with pytest.raises(KeyError, match="MQTT_PASSWORD"):
        load_config_with_credentials(yaml_path)
