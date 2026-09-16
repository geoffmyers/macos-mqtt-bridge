"""Tests for Phase E activity ticker."""

from __future__ import annotations

import asyncio
import subprocess
from unittest.mock import MagicMock

import pytest

from macos_bridge.phases.activity import ActivityTicker


@pytest.fixture
def fake_mqtt() -> MagicMock:
    m = MagicMock()
    m.host_slug = "test_host"
    m.sw_version = "0.1.0"
    m.host_friendly_name = "Test Host"
    return m


def _ticker(threshold: int = 5) -> ActivityTicker:
    return ActivityTicker(
        enabled=True,
        interval_seconds=5,
        host_slug="test_host",
        topic_prefix="macos",
        availability_topic="macos/test_host/status",
        input_active_threshold_seconds=threshold,
        camera_log_window_seconds=10,
        microphone_log_window_seconds=10,
    )


def _stub_subprocess(
    monkeypatch,
    *,
    idle_ns: int = 0,
    camera_log: str = "",
    mic_log: str = "",
    pmset_out: str = "",
):
    """Stub subprocess.run for the four shells the ticker invokes."""

    def fake_run(args, *a, **kw):
        if args[:2] == ["ioreg", "-c"] and args[2] == "IOHIDSystem":
            return subprocess.CompletedProcess(
                args, 0,
                stdout=f'    | "HIDIdleTime" = {idle_ns}\n',
                stderr="",
            )
        if args[:2] == ["log", "show"]:
            predicate = ""
            for i, tok in enumerate(args):
                if tok == "--predicate" and i + 1 < len(args):
                    predicate = args[i + 1]
            if "VDCAssistant" in predicate or "kCameraStream" in predicate:
                return subprocess.CompletedProcess(args, 0, stdout=camera_log, stderr="")
            if "AudioCapture" in predicate or "coremedia" in predicate:
                return subprocess.CompletedProcess(args, 0, stdout=mic_log, stderr="")
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
        if args[:2] == ["pmset", "-g"] and args[2] == "assertions":
            return subprocess.CompletedProcess(args, 0, stdout=pmset_out, stderr="")
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)


def test_phase_e_publishes_input_idle_seconds(monkeypatch, fake_mqtt: MagicMock):
    _stub_subprocess(monkeypatch, idle_ns=int(2.5e9))  # 2.5s idle
    ticker = _ticker(threshold=5)
    asyncio.run(ticker.run_once(fake_mqtt))
    publishes = {c.args[0]: c.args[1] for c in fake_mqtt.publish_state.call_args_list}
    assert publishes["macos/test_host/activity/input_idle_seconds"] == 2
    assert publishes["macos/test_host/activity/input_active"] == "ON"


def test_phase_e_input_inactive_when_idle_exceeds_threshold(monkeypatch, fake_mqtt: MagicMock):
    _stub_subprocess(monkeypatch, idle_ns=int(120e9))  # 120s idle
    ticker = _ticker(threshold=5)
    asyncio.run(ticker.run_once(fake_mqtt))
    publishes = {c.args[0]: c.args[1] for c in fake_mqtt.publish_state.call_args_list}
    assert publishes["macos/test_host/activity/input_active"] == "OFF"


def test_phase_e_camera_in_use_after_start_event(monkeypatch, fake_mqtt: MagicMock):
    log = (
        "2026-04-26 12:00:00.000 Df cmio[1] kCameraStreamStart\n"
    )
    _stub_subprocess(monkeypatch, camera_log=log)
    ticker = _ticker()
    asyncio.run(ticker.run_once(fake_mqtt))
    publishes = {c.args[0]: c.args[1] for c in fake_mqtt.publish_state.call_args_list}
    assert publishes["macos/test_host/activity/camera_in_use"] == "ON"


def test_phase_e_camera_off_when_stop_is_more_recent(monkeypatch, fake_mqtt: MagicMock):
    log = (
        "2026-04-26 12:00:00 Df cmio[1] kCameraStreamStart\n"
        "2026-04-26 12:01:00 Df cmio[1] kCameraStreamStop\n"
    )
    _stub_subprocess(monkeypatch, camera_log=log)
    ticker = _ticker()
    asyncio.run(ticker.run_once(fake_mqtt))
    publishes = {c.args[0]: c.args[1] for c in fake_mqtt.publish_state.call_args_list}
    assert publishes["macos/test_host/activity/camera_in_use"] == "OFF"


def test_phase_e_microphone_in_use_after_audiocapture_start(monkeypatch, fake_mqtt: MagicMock):
    log = (
        "2026-04-26 12:00:00 Df coremedia[1] AudioCapture started\n"
    )
    _stub_subprocess(monkeypatch, mic_log=log)
    ticker = _ticker()
    asyncio.run(ticker.run_once(fake_mqtt))
    publishes = {c.args[0]: c.args[1] for c in fake_mqtt.publish_state.call_args_list}
    assert publishes["macos/test_host/activity/microphone_in_use"] == "ON"


def test_phase_e_audio_playing_detects_coreaudiod_assertion(monkeypatch, fake_mqtt: MagicMock):
    pmset = (
        "Assertion status system-wide:\n"
        "   PreventUserIdleSystemSleep    1\n"
        "Listed by owning process:\n"
        "   pid 555(coreaudiod): [...] PreventUserIdleSystemSleep named: \"Audio output\"\n"
    )
    _stub_subprocess(monkeypatch, pmset_out=pmset)
    ticker = _ticker()
    asyncio.run(ticker.run_once(fake_mqtt))
    publishes = {c.args[0]: c.args[1] for c in fake_mqtt.publish_state.call_args_list}
    assert publishes["macos/test_host/activity/audio_playing"] == "ON"


def test_phase_e_audio_off_when_no_coreaudiod_assertion(monkeypatch, fake_mqtt: MagicMock):
    pmset = "Assertion status system-wide:\n   pid 1(launchd): [...] BackgroundTask\n"
    _stub_subprocess(monkeypatch, pmset_out=pmset)
    ticker = _ticker()
    asyncio.run(ticker.run_once(fake_mqtt))
    publishes = {c.args[0]: c.args[1] for c in fake_mqtt.publish_state.call_args_list}
    assert publishes["macos/test_host/activity/audio_playing"] == "OFF"


def test_phase_e_publishes_five_discovery_configs(fake_mqtt: MagicMock):
    ticker = _ticker()
    asyncio.run(ticker.publish_discovery(fake_mqtt))
    assert fake_mqtt.publish_discovery.call_count == 5
    components = [c.kwargs["component"] for c in fake_mqtt.publish_discovery.call_args_list]
    assert components.count("sensor") == 1  # input_idle_seconds
    assert components.count("binary_sensor") == 4  # camera, mic, audio, input_active
    unique_ids = {c.kwargs["unique_id"] for c in fake_mqtt.publish_discovery.call_args_list}
    assert unique_ids == {
        "macos_test_host_activity_input_idle_seconds",
        "macos_test_host_activity_input_active",
        "macos_test_host_activity_camera_in_use",
        "macos_test_host_activity_microphone_in_use",
        "macos_test_host_activity_audio_playing",
    }
