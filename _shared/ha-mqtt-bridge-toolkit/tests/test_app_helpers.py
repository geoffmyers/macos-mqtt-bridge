"""Tests for the process-startup helpers."""

import logging

import ha_mqtt_bridge
from ha_mqtt_bridge.app_helpers import (
    configure_logging,
    register_github_error_reporter,
)


def _capture_basicconfig(monkeypatch):
    """Stub out logging.basicConfig and return a dict of the kwargs it gets.

    basicConfig has a process-global side effect (and is a one-shot no-op
    once the root logger has handlers, e.g. under pytest's log capture), so
    we assert on what configure_logging *passes* rather than on root state.
    """
    captured: dict = {}
    monkeypatch.setattr(logging, "basicConfig", lambda **kw: captured.update(kw))
    return captured


def test_configure_logging_returns_named_logger(monkeypatch):
    _capture_basicconfig(monkeypatch)
    log = configure_logging("widget-bridge", "INFO")
    assert isinstance(log, logging.Logger)
    assert log.name == "widget-bridge"


def test_configure_logging_resolves_level_name(monkeypatch):
    captured = _capture_basicconfig(monkeypatch)
    configure_logging("widget-bridge", "WARNING")
    assert captured["level"] == logging.WARNING


def test_configure_logging_is_case_insensitive(monkeypatch):
    captured = _capture_basicconfig(monkeypatch)
    configure_logging("widget-bridge", "debug")
    assert captured["level"] == logging.DEBUG


def test_configure_logging_unknown_level_falls_back_to_info(monkeypatch):
    captured = _capture_basicconfig(monkeypatch)
    configure_logging("widget-bridge", "NOPE")
    assert captured["level"] == logging.INFO


def test_configure_logging_uses_uniform_format(monkeypatch):
    captured = _capture_basicconfig(monkeypatch)
    configure_logging("widget-bridge", "INFO")
    assert captured["format"] == "%(asctime)s %(levelname)s %(message)s"


def test_register_github_error_reporter_is_noop_without_package():
    # python_github_error_reporter is not a toolkit dependency, so this must
    # silently no-op rather than raise (the local-dev / unset-token path).
    register_github_error_reporter("widget-bridge")


def test_helpers_exported_from_package_root():
    assert ha_mqtt_bridge.configure_logging is configure_logging
    assert (
        ha_mqtt_bridge.register_github_error_reporter
        is register_github_error_reporter
    )
