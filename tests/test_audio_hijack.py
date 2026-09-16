"""Tests for AudioHijackController.

Verifies AppleScript dispatch, auto_stop behaviour, per-app session
mapping (Zoom/Teams/FaceTime/browser/phone), and phone event routing.
"""

from __future__ import annotations

from unittest.mock import MagicMock, call, patch

import pytest

from macos_bridge.audio_hijack import AudioHijackController


# ---- fixtures ---------------------------------------------------------------


def _ctrl(**overrides) -> AudioHijackController:
    defaults = dict(
        zoom_session="Record Zoom Meeting",
        teams_session="Record Microsoft Teams Meeting",
        facetime_session="Record FaceTime Call",
        browser_session="Record Browser Meeting",
        phone_session="Record Phone Call",
        auto_stop=True,
    )
    defaults.update(overrides)
    return AudioHijackController(**defaults)


def _mock_run_ok(mock_run: MagicMock) -> None:
    result = MagicMock()
    result.returncode = 0
    result.stderr = ""
    mock_run.return_value = result


def _mock_run_fail(mock_run: MagicMock) -> None:
    result = MagicMock()
    result.returncode = 1
    result.stderr = "Audio Hijack got an error"
    mock_run.return_value = result


# ---- AppleScript dispatch ---------------------------------------------------


class TestStartSession:
    def test_calls_osascript(self):
        ctrl = _ctrl()
        with patch("subprocess.run") as mock_run:
            _mock_run_ok(mock_run)
            ctrl.start_session("Record Zoom Meeting")
        mock_run.assert_called_once()
        cmd = mock_run.call_args[0][0]
        assert cmd[0] == "osascript"
        script = cmd[2]
        assert "Audio Hijack" in script
        assert "start" in script
        assert "Record Zoom Meeting" in script

    def test_tracks_started_session(self):
        ctrl = _ctrl()
        with patch("subprocess.run") as mock_run:
            _mock_run_ok(mock_run)
            ctrl.start_session("Record Zoom Meeting")
        assert "Record Zoom Meeting" in ctrl._started

    def test_failure_does_not_track_session(self):
        ctrl = _ctrl()
        with patch("subprocess.run") as mock_run:
            _mock_run_fail(mock_run)
            ctrl.start_session("Record Zoom Meeting")
        assert "Record Zoom Meeting" not in ctrl._started

    def test_osascript_not_found_does_not_raise(self):
        ctrl = _ctrl()
        with patch("subprocess.run", side_effect=FileNotFoundError):
            ctrl.start_session("Record Zoom Meeting")  # must not raise
        assert "Record Zoom Meeting" not in ctrl._started


class TestStopSession:
    def test_auto_stop_skips_unstarted_session(self):
        ctrl = _ctrl(auto_stop=True)
        with patch("subprocess.run") as mock_run:
            ctrl.stop_session("Record Zoom Meeting")
        mock_run.assert_not_called()

    def test_auto_stop_false_stops_any_session(self):
        ctrl = _ctrl(auto_stop=False)
        with patch("subprocess.run") as mock_run:
            _mock_run_ok(mock_run)
            ctrl.stop_session("Record Zoom Meeting")
        mock_run.assert_called_once()

    def test_stops_session_bridge_started(self):
        ctrl = _ctrl()
        with patch("subprocess.run") as mock_run:
            _mock_run_ok(mock_run)
            ctrl.start_session("Record Zoom Meeting")
            ctrl.stop_session("Record Zoom Meeting")
        assert mock_run.call_count == 2
        stop_script = mock_run.call_args_list[1][0][0][2]
        assert "stop" in stop_script
        assert "Record Zoom Meeting" in stop_script

    def test_stop_removes_from_started_set(self):
        ctrl = _ctrl()
        with patch("subprocess.run") as mock_run:
            _mock_run_ok(mock_run)
            ctrl.start_session("Record Zoom Meeting")
            ctrl.stop_session("Record Zoom Meeting")
        assert "Record Zoom Meeting" not in ctrl._started


# ---- per-app meeting callbacks ----------------------------------------------


class TestOnMeetingStarted:
    @pytest.mark.parametrize("friendly_app,expected_session", [
        ("Zoom", "Record Zoom Meeting"),
        ("Microsoft Teams", "Record Microsoft Teams Meeting"),
        ("Apple FaceTime", "Record FaceTime Call"),
        ("Google Meet", "Record Browser Meeting"),
    ])
    def test_starts_correct_session(self, friendly_app, expected_session):
        ctrl = _ctrl()
        with patch.object(ctrl, "start_session") as mock_start:
            ctrl.on_meeting_started(friendly_app)
        mock_start.assert_called_once_with(expected_session)

    def test_unknown_app_is_noop(self):
        ctrl = _ctrl()
        with patch.object(ctrl, "start_session") as mock_start:
            ctrl.on_meeting_started("Spotify")
        mock_start.assert_not_called()

    def test_unconfigured_session_is_noop(self):
        ctrl = _ctrl(zoom_session=None)
        with patch.object(ctrl, "start_session") as mock_start:
            ctrl.on_meeting_started("Zoom")
        mock_start.assert_not_called()


class TestOnMeetingEnded:
    @pytest.mark.parametrize("friendly_app,expected_session", [
        ("Zoom", "Record Zoom Meeting"),
        ("Microsoft Teams", "Record Microsoft Teams Meeting"),
        ("Apple FaceTime", "Record FaceTime Call"),
        ("Google Meet", "Record Browser Meeting"),
    ])
    def test_stops_correct_session(self, friendly_app, expected_session):
        ctrl = _ctrl()
        with patch.object(ctrl, "stop_session") as mock_stop:
            ctrl.on_meeting_ended(friendly_app)
        mock_stop.assert_called_once_with(expected_session)

    def test_unknown_app_is_noop(self):
        ctrl = _ctrl()
        with patch.object(ctrl, "stop_session") as mock_stop:
            ctrl.on_meeting_ended("Spotify")
        mock_stop.assert_not_called()


# ---- phone event routing ----------------------------------------------------


class TestOnPhoneEvent:
    def test_ringing_starts_phone_session(self):
        ctrl = _ctrl()
        with patch.object(ctrl, "start_session") as mock_start:
            ctrl.on_phone_event("phone/ringing")
        mock_start.assert_called_once_with("Record Phone Call")

    def test_outgoing_started_starts_phone_session(self):
        ctrl = _ctrl()
        with patch.object(ctrl, "start_session") as mock_start:
            ctrl.on_phone_event("phone/outgoing_started")
        mock_start.assert_called_once_with("Record Phone Call")

    def test_realtime_ended_stops_phone_session(self):
        ctrl = _ctrl()
        with patch.object(ctrl, "stop_session") as mock_stop:
            ctrl.on_phone_event("phone/realtime_ended")
        mock_stop.assert_called_once_with("Record Phone Call")

    def test_other_phone_events_are_noop(self):
        ctrl = _ctrl()
        with patch.object(ctrl, "start_session") as mock_start:
            with patch.object(ctrl, "stop_session") as mock_stop:
                ctrl.on_phone_event("phone/connected")
        mock_start.assert_not_called()
        mock_stop.assert_not_called()

    def test_no_phone_session_configured(self):
        ctrl = _ctrl(phone_session=None)
        with patch.object(ctrl, "start_session") as mock_start:
            ctrl.on_phone_event("phone/ringing")
        mock_start.assert_not_called()
