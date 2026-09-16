"""Unit tests for the Swift CallObserver subprocess wrapper.

The Swift binary itself can only run on macOS, so these tests stub out the
subprocess and verify our line-parsing + event-mapping logic.
"""

from __future__ import annotations

import json
from collections.abc import Callable

from macos_bridge.realtime_calls import _EVENT_MAP, RealtimeCallObserver


def _make_observer(emit: Callable[[str, dict], None]) -> RealtimeCallObserver:
    # Binary doesn't need to exist for the parser tests.
    return RealtimeCallObserver(binary_path="/nonexistent/CallObserver", emit=emit)


def test_event_map_covers_all_helper_states():
    expected = {
        "call_incoming_ringing",
        "call_outgoing_started",
        "call_connected",
        "call_ended",
    }
    assert set(_EVENT_MAP.keys()) == expected
    # Every mapped event path should be in the phone/* namespace.
    for path in _EVENT_MAP.values():
        assert path.startswith("phone/")


def test_handle_line_emits_mapped_events():
    seen = []
    obs = _make_observer(lambda t, p: seen.append((t, p)))
    obs._handle_line(json.dumps({
        "event": "call_incoming_ringing",
        "uuid": "ABC-123",
        "outgoing": False,
        "on_hold": False,
        "ts": "2026-05-10T12:00:00Z",
    }))
    assert seen == [("phone/ringing", {
        "uuid": "ABC-123",
        "outgoing": False,
        "on_hold": False,
        "ts": "2026-05-10T12:00:00Z",
    })]


def test_handle_line_emits_connected_and_ended():
    seen = []
    obs = _make_observer(lambda t, p: seen.append(t))
    obs._handle_line(json.dumps({"event": "call_connected", "uuid": "x"}))
    obs._handle_line(json.dumps({"event": "call_ended", "uuid": "x", "was_connected": True}))
    assert seen == ["phone/connected", "phone/realtime_ended"]


def test_observer_started_is_logged_not_emitted():
    seen = []
    obs = _make_observer(lambda t, p: seen.append(t))
    obs._handle_line(json.dumps({"event": "observer_started", "existing_calls": 0}))
    assert seen == []


def test_unrecognized_event_is_skipped():
    seen = []
    obs = _make_observer(lambda t, p: seen.append(t))
    obs._handle_line(json.dumps({"event": "something_new"}))
    assert seen == []


def test_malformed_json_does_not_raise():
    seen = []
    obs = _make_observer(lambda t, p: seen.append(t))
    obs._handle_line("not json at all")
    obs._handle_line("")
    assert seen == []


def test_start_with_missing_binary_does_not_start_thread():
    """When the binary doesn't exist, start() should log a warning and return
    without spawning a thread."""
    obs = _make_observer(lambda t, p: None)
    obs.start()
    assert obs._thread is None
