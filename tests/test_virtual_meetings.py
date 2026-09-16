"""Tests for the virtual_meetings phase ticker.

The detection logic parses ``pmset -g assertions`` output. Tests stub
``_run_pmset_assertions`` to inject controlled samples and assert the
state machine produces the right published topics.
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest

from macos_bridge.phases import virtual_meetings
from macos_bridge.phases.virtual_meetings import VirtualMeetingsTicker


# ---- realistic pmset output samples --------------------------------------


_PMSET_IDLE = """\
2026-05-10 19:30:00 -0500
Assertion status system-wide:
   PreventUserIdleDisplaySleep    1
Listed by owning process:
   pid 286(WindowServer): [0x0003c92700098e71] 00:48:02 UserIsActive named: "com.apple.iohideventsystem.queue.tickle"
   pid 1002(Dock): [0x0003c96e0005958d] 00:48:05 NoDisplaySleepAssertion named: "Turn off display sleep due to Expose hot corner"
"""


def _pmset_with(process: str, assertion_type: str, name: str) -> str:
    return f"""\
2026-05-10 19:30:00 -0500
Listed by owning process:
   pid 286(WindowServer): [0x0001] 00:48:02 UserIsActive named: "noise"
   pid 1234({process}): [0x0002] 00:01:00 {assertion_type} named: "{name}"
"""


def _pmset_coreaudiod_contexts(count: int, style: str = "indexed") -> str:
    """Build a pmset output with ``count`` coreaudiod audio-context
    assertions. macOS emits several name shapes — ``style`` picks one:

      "indexed"   — com.apple.audio.context<N>.preventuseridledisplaysleep
      "devices"   — com.apple.audio.BuiltInMicrophoneDevice / BuiltInSpeakerDevice
                    / AVVCAggregateDevice (rotates through the three)
    """
    lines = [
        "2026-05-12 10:40:00 -0500",
        "Listed by owning process:",
        '   pid 286(WindowServer): [0x0001] 00:48:02 UserIsActive named: "noise"',
    ]
    devices = [
        "BuiltInMicrophoneDevice.context.preventuseridlesleep",
        "BuiltInSpeakerDevice.context.preventuseridlesleep",
        "AVVCAggregateDevice-1045-282475249.context.preventuseridlesleep",
    ]
    for i in range(count):
        if style == "indexed":
            ctx = 147900 + i
            assertion_type = "PreventUserIdleDisplaySleep"
            name = f"com.apple.audio.context{ctx}.preventuseridledisplaysleep"
        elif style == "devices":
            assertion_type = "PreventUserIdleSystemSleep"
            name = f"com.apple.audio.{devices[i % len(devices)]}"
        else:
            raise ValueError(f"unknown style: {style}")
        lines.append(
            f'   pid 305(coreaudiod): [0x{1000 + i:04x}] 00:01:00 '
            f'{assertion_type} named: "{name}"'
        )
    return "\n".join(lines) + "\n"


# ---- helpers ---------------------------------------------------------------


@pytest.fixture
def fake_mqtt() -> MagicMock:
    m = MagicMock()
    m.host_slug = "test_host"
    m.sw_version = "0.1.0"
    return m


def _ticker(end_grace_ticks: int = 1) -> VirtualMeetingsTicker:
    """``end_grace_ticks=1`` makes the off transition immediate so tests
    don't have to spin extra ticks just to observe state changes."""
    return VirtualMeetingsTicker(
        enabled=True,
        interval_seconds=5,
        host_slug="test_host",
        topic_prefix="macos",
        availability_topic="macos/test_host/status",
        end_grace_ticks=end_grace_ticks,
    )


def _run_tick(ticker: VirtualMeetingsTicker, fake_mqtt: MagicMock) -> None:
    asyncio.run(ticker.run_once(fake_mqtt))


def _state_calls(fake_mqtt: MagicMock) -> dict[str, object]:
    return {c.args[0]: c.args[1] for c in fake_mqtt.publish_state.call_args_list}


# ---- tests ---------------------------------------------------------------


def test_no_meeting_publishes_off(monkeypatch: pytest.MonkeyPatch, fake_mqtt: MagicMock):
    monkeypatch.setattr(virtual_meetings, "_run_pmset_assertions", lambda: _PMSET_IDLE)
    ticker = _ticker()
    _run_tick(ticker, fake_mqtt)
    assert _state_calls(fake_mqtt)["macos/test_host/virtual_meeting/in_progress"] == "OFF"


def test_zoom_meeting_detected_and_app_label_correct(
    monkeypatch: pytest.MonkeyPatch, fake_mqtt: MagicMock
):
    monkeypatch.setattr(
        virtual_meetings,
        "_run_pmset_assertions",
        lambda: _pmset_with("zoom.us", "PreventUserIdleDisplaySleep", "Active Meeting"),
    )
    ticker = _ticker()
    _run_tick(ticker, fake_mqtt)
    calls = _state_calls(fake_mqtt)
    assert calls["macos/test_host/virtual_meeting/in_progress"] == "ON"
    assert calls["macos/test_host/virtual_meeting/last_application"] == "Zoom"
    # last_started_at gets set at meeting start (used by HA to compute elapsed).
    assert "macos/test_host/virtual_meeting/last_started_at" in calls


def test_microsoft_teams_native_app_detected(
    monkeypatch: pytest.MonkeyPatch, fake_mqtt: MagicMock
):
    monkeypatch.setattr(
        virtual_meetings,
        "_run_pmset_assertions",
        lambda: _pmset_with("Microsoft Teams", "PreventUserIdleDisplaySleep", "Meeting"),
    )
    ticker = _ticker()
    _run_tick(ticker, fake_mqtt)
    calls = _state_calls(fake_mqtt)
    assert calls["macos/test_host/virtual_meeting/in_progress"] == "ON"
    assert calls["macos/test_host/virtual_meeting/last_application"] == "Microsoft Teams"


def test_facetime_via_callservicesd_detected(
    monkeypatch: pytest.MonkeyPatch, fake_mqtt: MagicMock
):
    monkeypatch.setattr(
        virtual_meetings,
        "_run_pmset_assertions",
        lambda: _pmset_with(
            "callservicesd", "PreventUserIdleDisplaySleep", "Telephony - Call in Progress"
        ),
    )
    ticker = _ticker()
    _run_tick(ticker, fake_mqtt)
    calls = _state_calls(fake_mqtt)
    assert calls["macos/test_host/virtual_meeting/in_progress"] == "ON"
    assert calls["macos/test_host/virtual_meeting/last_application"] == "Apple FaceTime"


def test_browser_assertion_without_meeting_keyword_is_ignored(
    monkeypatch: pytest.MonkeyPatch, fake_mqtt: MagicMock
):
    """A browser playing fullscreen YouTube takes a display-sleep assertion
    too — only assertions with media/meeting keywords should count."""
    monkeypatch.setattr(
        virtual_meetings,
        "_run_pmset_assertions",
        lambda: _pmset_with(
            "Brave Browser", "PreventUserIdleDisplaySleep", "Fullscreen video playback"
        ),
    )
    ticker = _ticker()
    _run_tick(ticker, fake_mqtt)
    assert _state_calls(fake_mqtt)["macos/test_host/virtual_meeting/in_progress"] == "OFF"


def test_browser_with_meeting_keyword_qualifies(
    monkeypatch: pytest.MonkeyPatch, fake_mqtt: MagicMock
):
    monkeypatch.setattr(
        virtual_meetings,
        "_run_pmset_assertions",
        lambda: _pmset_with(
            "Brave Browser", "PreventUserIdleDisplaySleep", "WebRTC has active PeerConnections"
        ),
    )
    ticker = _ticker()
    _run_tick(ticker, fake_mqtt)
    calls = _state_calls(fake_mqtt)
    assert calls["macos/test_host/virtual_meeting/in_progress"] == "ON"
    assert calls["macos/test_host/virtual_meeting/last_application"] == "Google Meet"


def test_meeting_end_records_duration(
    monkeypatch: pytest.MonkeyPatch, fake_mqtt: MagicMock
):
    """Going from detected → not detected should record ended_at and duration."""
    pmset_state = {"text": _pmset_with("zoom.us", "PreventUserIdleDisplaySleep", "Meeting")}
    monkeypatch.setattr(
        virtual_meetings, "_run_pmset_assertions", lambda: pmset_state["text"]
    )
    ticker = _ticker(end_grace_ticks=1)

    _run_tick(ticker, fake_mqtt)  # meeting starts
    pmset_state["text"] = _PMSET_IDLE
    fake_mqtt.publish_state.reset_mock()
    _run_tick(ticker, fake_mqtt)  # meeting ends (grace=1 → flips immediately)

    calls = _state_calls(fake_mqtt)
    assert calls["macos/test_host/virtual_meeting/in_progress"] == "OFF"
    assert "macos/test_host/virtual_meeting/last_ended_at" in calls
    assert "macos/test_host/virtual_meeting/last_duration" in calls
    assert calls["macos/test_host/virtual_meeting/last_duration"] >= 0


def test_end_grace_smooths_momentary_gap(
    monkeypatch: pytest.MonkeyPatch, fake_mqtt: MagicMock
):
    """A single negative tick shouldn't end the meeting when grace>1."""
    pmset_state = {"text": _pmset_with("zoom.us", "PreventUserIdleDisplaySleep", "Meeting")}
    monkeypatch.setattr(
        virtual_meetings, "_run_pmset_assertions", lambda: pmset_state["text"]
    )
    ticker = _ticker(end_grace_ticks=3)

    _run_tick(ticker, fake_mqtt)  # tick 1: meeting on
    pmset_state["text"] = _PMSET_IDLE
    fake_mqtt.publish_state.reset_mock()
    _run_tick(ticker, fake_mqtt)  # tick 2: 1 negative — still on
    assert _state_calls(fake_mqtt).get("macos/test_host/virtual_meeting/in_progress") in (None, "ON")

    pmset_state["text"] = _pmset_with("zoom.us", "PreventUserIdleDisplaySleep", "Meeting")
    fake_mqtt.publish_state.reset_mock()
    _run_tick(ticker, fake_mqtt)  # tick 3: positive again — streak resets
    # Republish suppressed when state hasn't changed; assertion is only that
    # it's still on (no OFF was emitted in tick 2).
    last_in_progress = ticker._published_in_progress
    assert last_in_progress == "ON"


def test_coreaudiod_inferred_teams_meeting(
    monkeypatch: pytest.MonkeyPatch, fake_mqtt: MagicMock
):
    """Teams 2.0 on macOS Tahoe routes its audio assertions through
    coreaudiod, not its own process. When coreaudiod has multiple audio
    contexts AND MSTeams is running, infer a Teams meeting."""
    monkeypatch.setattr(
        virtual_meetings, "_run_pmset_assertions",
        lambda: _pmset_coreaudiod_contexts(5),
    )
    monkeypatch.setattr(
        virtual_meetings, "_detect_running_native_meeting_app",
        lambda: ("Microsoft Teams", "native_inferred", "MSTeams"),
    )
    ticker = _ticker()
    _run_tick(ticker, fake_mqtt)
    calls = _state_calls(fake_mqtt)
    assert calls["macos/test_host/virtual_meeting/in_progress"] == "ON"
    assert calls["macos/test_host/virtual_meeting/last_application"] == "Microsoft Teams"


def test_coreaudiod_single_context_does_not_trigger(
    monkeypatch: pytest.MonkeyPatch, fake_mqtt: MagicMock
):
    """One coreaudiod context = single-stream playback (music, etc.).
    Don't flip in_progress=ON just because a meeting app happens to be
    running while music plays."""
    monkeypatch.setattr(
        virtual_meetings, "_run_pmset_assertions",
        lambda: _pmset_coreaudiod_contexts(1),
    )
    # Even if MSTeams is running, single-stream audio shouldn't trigger.
    monkeypatch.setattr(
        virtual_meetings, "_detect_running_native_meeting_app",
        lambda: ("Microsoft Teams", "native_inferred", "MSTeams"),
    )
    ticker = _ticker()
    _run_tick(ticker, fake_mqtt)
    assert _state_calls(fake_mqtt)["macos/test_host/virtual_meeting/in_progress"] == "OFF"


def test_coreaudiod_active_but_no_meeting_app_running(
    monkeypatch: pytest.MonkeyPatch, fake_mqtt: MagicMock
):
    """Music playback (multiple coreaudiod contexts possible if user has
    multiple players running) without any known meeting app — not a
    meeting."""
    monkeypatch.setattr(
        virtual_meetings, "_run_pmset_assertions",
        lambda: _pmset_coreaudiod_contexts(4),
    )
    monkeypatch.setattr(
        virtual_meetings, "_detect_running_native_meeting_app", lambda: None,
    )
    ticker = _ticker()
    _run_tick(ticker, fake_mqtt)
    assert _state_calls(fake_mqtt)["macos/test_host/virtual_meeting/in_progress"] == "OFF"


def test_direct_assertion_wins_over_coreaudiod_inference(
    monkeypatch: pytest.MonkeyPatch, fake_mqtt: MagicMock
):
    """When both signals fire (direct Zoom assertion AND coreaudiod
    contexts AND MSTeams running), the direct match should win."""
    pmset_text = (
        _pmset_with("zoom.us", "PreventUserIdleDisplaySleep", "Meeting")
        + _pmset_coreaudiod_contexts(5).split("Listed by owning process:\n", 1)[1]
    )
    monkeypatch.setattr(virtual_meetings, "_run_pmset_assertions", lambda: pmset_text)
    # If this is called, the test still passes — but the direct match
    # path should short-circuit before consulting pgrep.
    monkeypatch.setattr(
        virtual_meetings, "_detect_running_native_meeting_app",
        lambda: ("Microsoft Teams", "native_inferred", "MSTeams"),
    )
    ticker = _ticker()
    _run_tick(ticker, fake_mqtt)
    last_app_topic = "macos/test_host/virtual_meeting/last_application"
    assert _state_calls(fake_mqtt)[last_app_topic] == "Zoom"


def test_count_coreaudiod_audio_contexts_ignores_other_processes():
    text = (
        _pmset_with("zoom.us", "PreventUserIdleDisplaySleep", "Meeting")
        + _pmset_coreaudiod_contexts(3).split("Listed by owning process:\n", 1)[1]
    )
    # The zoom.us assertion is NOT a coreaudiod context — only the 3
    # coreaudiod entries should be counted.
    assert virtual_meetings._count_coreaudiod_audio_contexts(text) == 3


def test_count_coreaudiod_audio_contexts_recognizes_device_named_assertions():
    """macOS also emits assertion names like
    ``com.apple.audio.BuiltInMicrophoneDevice.context.preventuseridlesleep``
    (per-device) in addition to the ``contextN`` form (per-stream). Both
    must count."""
    text = _pmset_coreaudiod_contexts(3, style="devices")
    assert virtual_meetings._count_coreaudiod_audio_contexts(text) == 3


def test_browser_webrtc_noidlesleepassertion_qualifies(
    monkeypatch: pytest.MonkeyPatch, fake_mqtt: MagicMock
):
    """Brave/Chromium emits ``NoIdleSleepAssertion named: "WebRTC has
    active PeerConnections"`` during Google Meet calls — the older
    assertion-type constant, but valid evidence."""
    monkeypatch.setattr(
        virtual_meetings, "_run_pmset_assertions",
        lambda: _pmset_with(
            "Brave Browser", "NoIdleSleepAssertion", "WebRTC has active PeerConnections",
        ),
    )
    ticker = _ticker()
    _run_tick(ticker, fake_mqtt)
    calls = _state_calls(fake_mqtt)
    assert calls["macos/test_host/virtual_meeting/in_progress"] == "ON"
    assert calls["macos/test_host/virtual_meeting/last_application"] == "Google Meet"


def test_published_discovery_includes_six_entities(fake_mqtt: MagicMock):
    ticker = _ticker()
    asyncio.run(ticker.publish_discovery(fake_mqtt))
    assert fake_mqtt.publish_discovery.call_count == 6
    unique_ids = {c.kwargs["unique_id"] for c in fake_mqtt.publish_discovery.call_args_list}
    assert unique_ids == {
        "macos_test_host_virtual_meeting_in_progress",
        "macos_test_host_virtual_meeting_last_application",
        "macos_test_host_virtual_meeting_last_started_at",
        "macos_test_host_virtual_meeting_last_ended_at",
        "macos_test_host_virtual_meeting_last_duration",
        "macos_test_host_virtual_meeting_last_title",
    }
