"""Config loading + path expansion."""

from __future__ import annotations

from pathlib import Path

from macos_bridge.config import load_config


def test_example_config_validates():
    here = Path(__file__).resolve()
    example = here.parents[1] / "config.example.yaml"
    cfg = load_config(example)

    assert cfg.mqtt.host
    assert cfg.bridge.hostname  # default-resolved if omitted
    assert cfg.sources.messages.enabled
    assert cfg.sources.calls.enabled
    assert cfg.sources.voicemail.enabled


def test_path_expansion_resolves_tilde_and_env(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    here = Path(__file__).resolve()
    example = here.parents[1] / "config.example.yaml"
    cfg = load_config(example)
    paths = cfg.expanded_paths()
    assert str(tmp_path) in paths["log_path"]
    assert str(tmp_path) in paths["state_path"]
    assert str(tmp_path) in paths["messages_db"]
