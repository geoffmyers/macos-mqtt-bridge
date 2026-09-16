"""Tests for the inbound HA controls module.

The shell-execution paths (_run / _osascript) are stubbed out so the
tests don't actually change system volume, lock the screen, or speak
"hello" out the speakers on the developer's Mac.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from macos_bridge import controls
from macos_bridge.config import Config
from macos_bridge.controls import ControlsHandler, _build_controls


def _config(**controls_overrides) -> Config:
    base = {
        "bridge": {"hostname": "test_host"},
        "mqtt": {
            "host": "broker.lan",
            "port": 8883,
            "tls": True,
            "ca_file": "/etc/ssl/cert.pem",
        },
    }
    if controls_overrides:
        base["controls"] = controls_overrides
    return Config.model_validate(base)


@pytest.fixture
def captured_runs(monkeypatch: pytest.MonkeyPatch) -> list:
    """Capture every shell invocation issued through controls._run /
    controls._osascript so we can assert on the exact command without
    actually running it."""
    calls: list = []

    def fake_run(args, *, timeout=5.0):
        calls.append(("run", tuple(args), timeout))
        return 0, "", ""

    def fake_osascript(script, *, timeout=5.0):
        calls.append(("osascript", script, timeout))
        return 0, "", ""

    monkeypatch.setattr(controls, "_run", fake_run)
    monkeypatch.setattr(controls, "_osascript", fake_osascript)
    return calls


def _publisher() -> MagicMock:
    pub = MagicMock()
    pub.host_device_block.return_value = {"identifiers": ["x"]}
    return pub


# ---- per-control flag plumbing ---------------------------------------------

def test_destructive_controls_default_to_disabled():
    """The whole point of opt-in: a fresh config should NOT advertise
    sleep / restart / shutdown buttons. Anyone who can publish to the
    broker would otherwise own the machine."""
    cfg = _config()
    built = _build_controls(cfg.controls)
    slugs = {c.slug for c in built}
    assert "sleep" not in slugs
    assert "restart" not in slugs
    assert "shutdown" not in slugs
    # Safe defaults SHOULD be present.
    assert "volume" in slugs
    assert "mute" in slugs
    assert "lock_screen" in slugs


def test_destructive_controls_appear_when_opted_in():
    cfg = _config(sleep=True, restart=True, shutdown=True)
    slugs = {c.slug for c in _build_controls(cfg.controls)}
    assert "sleep" in slugs
    assert "restart" in slugs
    assert "shutdown" in slugs


def test_disabling_a_control_removes_it():
    cfg = _config(volume=False, mute=False)
    slugs = {c.slug for c in _build_controls(cfg.controls)}
    assert "volume" not in slugs
    assert "mute" not in slugs


# ---- handler dispatch ------------------------------------------------------

def test_volume_handler_clamps_and_runs_osascript(captured_runs: list):
    cfg = _config()
    handler = ControlsHandler(cfg=cfg, host_slug="test_host", publisher=_publisher())
    volume_control = next(c for c in handler.controls if c.slug == "volume")

    # 50%
    volume_control.handler(b"50", handler)
    # Out-of-range is clamped to 0..100 to keep `osascript` happy.
    volume_control.handler(b"-30", handler)
    volume_control.handler(b"250", handler)
    # Garbage payload is silently dropped (no command issued).
    volume_control.handler(b"garbage", handler)

    osascript_calls = [c for c in captured_runs if c[0] == "osascript"]
    assert len(osascript_calls) == 3
    assert osascript_calls[0][1] == "set volume output volume 50"
    assert osascript_calls[1][1] == "set volume output volume 0"
    assert osascript_calls[2][1] == "set volume output volume 100"


def test_mute_handler_translates_on_off(captured_runs: list):
    handler = ControlsHandler(
        cfg=_config(), host_slug="test_host", publisher=_publisher()
    )
    mute = next(c for c in handler.controls if c.slug == "mute")

    mute.handler(b"ON", handler)
    mute.handler(b"OFF", handler)

    osascript_calls = [c for c in captured_runs if c[0] == "osascript"]
    assert "set volume output muted true" in osascript_calls[0][1]
    assert "set volume output muted false" in osascript_calls[1][1]


def test_lock_screen_runs_pmset(captured_runs: list):
    handler = ControlsHandler(
        cfg=_config(), host_slug="test_host", publisher=_publisher()
    )
    lock = next(c for c in handler.controls if c.slug == "lock_screen")
    lock.handler(b"PRESS", handler)

    run_calls = [c for c in captured_runs if c[0] == "run"]
    assert any(c[1] == ("pmset", "displaysleepnow") for c in run_calls)


def test_open_url_rejects_unsafe_schemes(captured_runs: list):
    handler = ControlsHandler(
        cfg=_config(), host_slug="test_host", publisher=_publisher()
    )
    open_url = next(c for c in handler.controls if c.slug == "open_url")

    # file:// could read sensitive paths via a misconfigured automation.
    open_url.handler(b"file:///etc/passwd", handler)
    # Random schemes are blocked.
    open_url.handler(b"javascript:alert(1)", handler)
    # Empty payload no-ops.
    open_url.handler(b"", handler)
    # http(s)://, mailto:, tel:, facetime://, imessage:// are allowed.
    open_url.handler(b"https://example.com", handler)
    open_url.handler(b"mailto:foo@example.com", handler)

    run_calls = [c for c in captured_runs if c[0] == "run"]
    open_calls = [c for c in run_calls if c[1][0] == "open"]
    assert len(open_calls) == 2
    assert open_calls[0][1] == ("open", "https://example.com")
    assert open_calls[1][1] == ("open", "mailto:foo@example.com")


def test_speak_passes_text_via_argv(captured_runs: list):
    """``say`` takes the text as a positional arg — argv-based (not
    shell-based) so a text containing single quotes / dollar signs /
    backticks can't escape into shell injection."""
    handler = ControlsHandler(
        cfg=_config(), host_slug="test_host", publisher=_publisher()
    )
    speak = next(c for c in handler.controls if c.slug == "speak")
    speak.handler(b"hello $(rm -rf /); world", handler)

    run_calls = [c for c in captured_runs if c[0] == "run"]
    say_calls = [c for c in run_calls if c[1][0] == "say"]
    assert len(say_calls) == 1
    # The dangerous-looking string is passed verbatim as argv[1] —
    # no shell interpolation happens.
    assert say_calls[0][1] == ("say", "hello $(rm -rf /); world")


def test_display_notification_escapes_quotes(captured_runs: list):
    """AppleScript string literals require backslash-escaped quotes
    and backslashes. Ensure user-supplied text with quotes doesn't
    break out of the literal."""
    handler = ControlsHandler(
        cfg=_config(), host_slug="test_host", publisher=_publisher()
    )
    notify = next(c for c in handler.controls if c.slug == "display_notification")
    notify.handler(b'evil " end-of-string', handler)

    osascript_calls = [c for c in captured_runs if c[0] == "osascript"]
    assert len(osascript_calls) == 1
    script = osascript_calls[0][1]
    # The user's quote should be backslash-escaped so the AppleScript
    # parser sees a single string literal rather than two.
    assert 'evil \\" end-of-string' in script


# ---- caffeinate manager ----------------------------------------------------

def test_caffeinate_handler_publishes_state(monkeypatch: pytest.MonkeyPatch):
    handler = ControlsHandler(
        cfg=_config(), host_slug="test_host", publisher=_publisher()
    )

    # Stub the subprocess.Popen so we don't actually fork caffeinate.
    spawned = []

    class _FakeProc:
        def __init__(self) -> None:
            self.pid = 99999
            self._alive = True

        def poll(self):
            return None if self._alive else 0

        def terminate(self):
            self._alive = False

        def wait(self, timeout=None):
            return 0

    def fake_popen(args, **_kwargs):
        spawned.append(args)
        return _FakeProc()

    monkeypatch.setattr(controls.subprocess, "Popen", fake_popen)

    caff = next(c for c in handler.controls if c.slug == "caffeinate")
    state = caff.handler(b"ON", handler)
    assert state == {"state": "ON"}
    assert spawned == [["caffeinate", "-di"]]
    assert handler.caffeinate.is_active

    state = caff.handler(b"OFF", handler)
    assert state == {"state": "OFF"}
    assert not handler.caffeinate.is_active


# ---- discovery + topic plumbing --------------------------------------------

def test_discovery_volume_reuses_system_state_topic():
    """The volume slider's state_topic should point at the topic the
    system_state ticker is already publishing to, so HA learns the
    current volume without the controls module having to poll."""
    handler = ControlsHandler(
        cfg=_config(), host_slug="test_host", publisher=_publisher()
    )
    pub = handler.publisher
    handler.publish_discovery()

    by_uid = {
        c.kwargs["unique_id"]: c.kwargs["payload"]
        for c in pub.publish_discovery.call_args_list
    }
    vol = by_uid["macos_test_host_volume"]
    assert vol["state_topic"] == "macos/test_host/system/volume_level"
    assert vol["command_topic"] == "macos/test_host/controls/volume/command"
    assert vol["min"] == 0 and vol["max"] == 100
    assert vol["unit_of_measurement"] == "%"

    mute = by_uid["macos_test_host_mute"]
    assert mute["state_topic"] == "macos/test_host/system/volume_muted"
    assert mute["payload_on"] == "ON"


def test_discovery_caffeinate_uses_own_state_topic():
    """Switches with no natural read-side counterpart need their own
    state topic so HA can render the current toggle position."""
    handler = ControlsHandler(
        cfg=_config(), host_slug="test_host", publisher=_publisher()
    )
    pub = handler.publisher
    handler.publish_discovery()

    by_uid = {
        c.kwargs["unique_id"]: c.kwargs["payload"]
        for c in pub.publish_discovery.call_args_list
    }
    caff = by_uid["macos_test_host_caffeinate"]
    assert caff["state_topic"] == "macos/test_host/controls/caffeinate/state"


def test_discovery_omits_state_topic_for_buttons_and_text():
    """Buttons and text inputs are write-only — HA doesn't expect a
    state_topic for them. Switches MUST have one (covered separately)."""
    handler = ControlsHandler(
        cfg=_config(sleep=True, restart=True, shutdown=True),
        host_slug="test_host", publisher=_publisher(),
    )
    pub = handler.publisher
    handler.publish_discovery()

    by_uid = {
        c.kwargs["unique_id"]: c.kwargs["payload"]
        for c in pub.publish_discovery.call_args_list
    }
    for slug in ("lock_screen", "sleep", "restart", "shutdown",
                 "media_next", "media_prev",
                 "display_notification", "speak", "open_url"):
        payload = by_uid[f"macos_test_host_{slug}"]
        assert "state_topic" not in payload, f"{slug} should not have state_topic"
        # All controls share the bridge's LWT so HA flips them to
        # Unavailable when the daemon disconnects.
        assert payload["availability_topic"] == "macos/status"


def test_screensaver_switch_reuses_system_state_topic():
    """Screensaver was a button; it's now a switch backed by the
    system_state ticker's screensaver_running topic so HA reflects
    external start/stop (idle timer, hot corner) without our handler
    publishing back."""
    handler = ControlsHandler(
        cfg=_config(), host_slug="test_host", publisher=_publisher()
    )
    pub = handler.publisher
    handler.publish_discovery()

    by_uid = {
        c.kwargs["unique_id"]: c.kwargs["payload"]
        for c in pub.publish_discovery.call_args_list
    }
    saver = by_uid["macos_test_host_screensaver"]
    assert saver["state_topic"] == "macos/test_host/system/screensaver_running"
    assert saver["payload_on"] == "ON"
    assert saver["payload_off"] == "OFF"
    assert saver["state_on"] == "ON"
    assert saver["state_off"] == "OFF"


def test_screensaver_switch_runs_open_on_then_killall_off(captured_runs: list):
    handler = ControlsHandler(
        cfg=_config(), host_slug="test_host", publisher=_publisher()
    )
    saver = next(c for c in handler.controls if c.slug == "screensaver")

    saver.handler(b"ON", handler)
    saver.handler(b"OFF", handler)

    run_calls = [c[1] for c in captured_runs if c[0] == "run"]
    assert ("open", "-a", "ScreenSaverEngine") in run_calls
    assert ("killall", "ScreenSaverEngine") in run_calls


def test_media_playing_switch_reuses_now_playing_state_topic():
    """Media playback toggles map to ON when state == 'Playing', OFF
    otherwise. The 5s now_playing cadence keeps HA snappy."""
    handler = ControlsHandler(
        cfg=_config(), host_slug="test_host", publisher=_publisher()
    )
    pub = handler.publisher
    handler.publish_discovery()

    by_uid = {
        c.kwargs["unique_id"]: c.kwargs["payload"]
        for c in pub.publish_discovery.call_args_list
    }
    media = by_uid["macos_test_host_media_playing"]
    assert media["state_topic"] == "macos/test_host/now_playing/state"
    # value_template collapses "Playing" -> ON; "Paused" / "Stopped" /
    # "None" -> OFF.
    assert "value == 'Playing'" in media["value_template"]
    assert media["state_on"] == "ON"
    assert media["state_off"] == "OFF"


def test_media_playing_switch_handler_dispatches_play_or_pause(
    monkeypatch: pytest.MonkeyPatch, captured_runs: list
):
    """ON -> nowplaying-cli play; OFF -> nowplaying-cli pause. When
    nowplaying-cli isn't installed, the AppleScript fallback runs
    the corresponding Music.app command."""
    monkeypatch.setattr(controls.shutil, "which", lambda _name: "/usr/local/bin/nowplaying-cli")
    handler = ControlsHandler(
        cfg=_config(), host_slug="test_host", publisher=_publisher()
    )
    media = next(c for c in handler.controls if c.slug == "media_playing")

    media.handler(b"ON", handler)
    media.handler(b"OFF", handler)

    run_calls = [c[1] for c in captured_runs if c[0] == "run"]
    assert ("nowplaying-cli", "play") in run_calls
    assert ("nowplaying-cli", "pause") in run_calls


def test_subscribe_registers_one_handler_per_control():
    handler = ControlsHandler(
        cfg=_config(), host_slug="test_host", publisher=_publisher()
    )
    pub = handler.publisher
    handler.subscribe()

    subscribed_topics = [c.args[0] for c in pub.subscribe.call_args_list]
    assert len(subscribed_topics) == len(handler.controls)
    for control in handler.controls:
        assert handler.command_topic(control) in subscribed_topics
