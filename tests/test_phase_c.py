"""Tests for Phase C focused-app ticker (lsappinfo-based)."""

from __future__ import annotations

import asyncio
import subprocess
from unittest.mock import MagicMock

import pytest

from macos_bridge.phases.focused_app import FocusedAppTicker


@pytest.fixture
def fake_mqtt() -> MagicMock:
    m = MagicMock()
    m.host_slug = "test_host"
    m.sw_version = "0.1.0"
    m.host_friendly_name = "Test Host"
    return m


@pytest.fixture(autouse=True)
def _stub_bundle_lookup(monkeypatch: pytest.MonkeyPatch):
    from macos_bridge import apps
    apps.bundle_id_to_app_name.cache_clear()
    monkeypatch.setattr(
        "macos_bridge.phases.focused_app.bundle_id_to_app_name",
        lambda bid: {
            "com.apple.Safari": "Safari",
            "com.apple.logic10": "Logic Pro",
            "com.tinyspeck.slackmacgap": "Slack",
        }.get(bid, bid or "none"),
    )


def _ticker() -> FocusedAppTicker:
    return FocusedAppTicker(
        enabled=True,
        poll_interval_seconds=0,
        host_slug="test_host",
        topic_prefix="macos",
        availability_topic="macos/test_host/status",
    )


def _stub_lsappinfo(
    monkeypatch,
    bundle_id: str | None,
    *,
    version: str = "1.2.3",
    arch: str = "ARM64",
    bundle_path: str = "/Applications/Stub.app",
    visible_count: int = 4,
    window_title: str | None = "Untitled — Stub",
    axdocument: str = "",
):
    """Stub lsappinfo + osascript subcommands the ticker uses.

    osascript is called with two distinct scripts (window title vs.
    AXDocument); we differentiate by inspecting the script source.
    """

    def fake_run(args, *a, **kw):
        if args[:2] == ["lsappinfo", "front"]:
            return subprocess.CompletedProcess(args, 0, stdout="ASN:0x0-0x123:\n", stderr="")
        if args[:2] == ["lsappinfo", "info"]:
            if bundle_id is None:
                return subprocess.CompletedProcess(args, 1, stdout="", stderr="")
            stdout = (
                f'"App" ASN:0x0-0x123: (in front)\n'
                f'    bundleID="{bundle_id}"\n'
                f'    bundle path="{bundle_path}"\n'
                f'    pid = 12345 type="Foreground" Version="{version}" '
                f'fileType="APPL" Arch={arch}\n'
                f"    checkin time = 2026/04/26 09:00:00 (X ago)\n"
            )
            return subprocess.CompletedProcess(args, 0, stdout=stdout, stderr="")
        if args[:2] == ["lsappinfo", "visibleProcessList"]:
            tokens = " ".join(f'ASN:0x0-0x{i:x}-"App{i}":' for i in range(visible_count))
            return subprocess.CompletedProcess(args, 0, stdout=tokens + "\n", stderr="")
        if args[:1] == ["osascript"]:
            script = args[2] if len(args) > 2 else ""
            # AXDocument vs window-title scripts differ in body
            if "AXDocument" in script:
                return subprocess.CompletedProcess(args, 0, stdout=axdocument + "\n", stderr="")
            if window_title is None:
                # Simulate Accessibility-denied (osascript exits non-zero)
                return subprocess.CompletedProcess(
                    args, 1, stdout="", stderr="execution error: not authorized"
                )
            return subprocess.CompletedProcess(args, 0, stdout=window_title + "\n", stderr="")
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)


def test_phase_c_publishes_focused_app_on_first_run(
    monkeypatch: pytest.MonkeyPatch, fake_mqtt: MagicMock
):
    _stub_lsappinfo(
        monkeypatch, "com.apple.logic10",
        version="11.1.0", arch="ARM64", visible_count=5,
        window_title="Logic Pro — My Song.logicx",
    )
    ticker = _ticker()
    asyncio.run(ticker.run_once(fake_mqtt))

    publishes = {c.args[0]: c.args[1] for c in fake_mqtt.publish_state.call_args_list}
    # Merged sensor: state of focus/app is now the friendly name (was bundle_id).
    # bundle_id remains accessible via the attrs payload.
    assert publishes["macos/test_host/focus/app"] == "Logic Pro"
    assert "macos/test_host/focus/app_friendly" not in publishes
    assert (
        publishes["macos/test_host/focus/window_title"]
        == "Logic Pro — My Song.logicx"
    )
    assert publishes["macos/test_host/visible_apps_count"] == 5

    attrs = {c.args[0]: c.args[1] for c in fake_mqtt.publish_attributes.call_args_list}
    payload = attrs["macos/test_host/focus/attrs"]
    assert payload["bundle_id"] == "com.apple.logic10"
    assert payload["app_version"] == "11.1.0"
    assert payload["app_arch"] == "arm64"
    assert payload["app_bundle_path"] == "/Applications/Stub.app"
    assert payload["app_launched_at"].startswith("2026-04-26T09:00:00")
    assert payload["window_title"] == "Logic Pro — My Song.logicx"


def test_phase_c_window_title_change_within_same_app(
    monkeypatch: pytest.MonkeyPatch, fake_mqtt: MagicMock
):
    """Switching tabs/files within the same app should republish window_title
    even though the bundle id hasn't changed.
    """
    _stub_lsappinfo(monkeypatch, "com.apple.Safari", window_title="Tab A — Safari")
    ticker = _ticker()
    asyncio.run(ticker.run_once(fake_mqtt))
    fake_mqtt.publish_state.reset_mock()

    _stub_lsappinfo(monkeypatch, "com.apple.Safari", window_title="Tab B — Safari")
    asyncio.run(ticker.run_once(fake_mqtt))

    topics = {c.args[0]: c.args[1] for c in fake_mqtt.publish_state.call_args_list}
    # Bundle didn't change → focus/app should NOT republish
    assert "macos/test_host/focus/app" not in topics
    # But window_title should
    assert topics["macos/test_host/focus/window_title"] == "Tab B — Safari"


def test_phase_c_does_not_republish_focus_when_unchanged(
    monkeypatch: pytest.MonkeyPatch, fake_mqtt: MagicMock
):
    _stub_lsappinfo(monkeypatch, "com.apple.Safari")
    ticker = _ticker()
    asyncio.run(ticker.run_once(fake_mqtt))
    fake_mqtt.publish_state.reset_mock()
    asyncio.run(ticker.run_once(fake_mqtt))
    # Visible-apps count publishes every tick; focus topics do NOT republish
    # when the focused bundle is unchanged.
    topics = [c.args[0] for c in fake_mqtt.publish_state.call_args_list]
    assert all("focus/" not in t for t in topics), topics
    assert any("visible_apps_count" in t for t in topics), topics


def test_phase_c_locked_screen_publishes_login_window(
    monkeypatch: pytest.MonkeyPatch, fake_mqtt: MagicMock
):
    _stub_lsappinfo(monkeypatch, "com.apple.loginwindow")
    ticker = _ticker()
    asyncio.run(ticker.run_once(fake_mqtt))
    publishes = {c.args[0]: c.args[1] for c in fake_mqtt.publish_state.call_args_list}
    # State of the merged sensor is the friendly "Login Window"; bundle_id
    # remains in the attrs payload for precise HA automation matches.
    assert publishes["macos/test_host/focus/app"] == "Login Window"
    attrs = {c.args[0]: c.args[1] for c in fake_mqtt.publish_attributes.call_args_list}
    assert attrs["macos/test_host/focus/attrs"]["bundle_id"] == "com.apple.loginwindow"


def test_phase_c_publishes_six_discovery_configs(fake_mqtt: MagicMock):
    ticker = _ticker()
    asyncio.run(ticker.publish_discovery(fake_mqtt))
    # 6 sensors: app (merged), app_changed_at, window_title, file_path,
    # file_name, visible_apps_count. The legacy app_friendly sensor was
    # merged into app and is removed.
    assert fake_mqtt.publish_discovery.call_count == 6
    unique_ids = {c.kwargs["unique_id"] for c in fake_mqtt.publish_discovery.call_args_list}
    assert unique_ids == {
        "macos_test_host_focused_app",
        "macos_test_host_focused_app_changed_at",
        "macos_test_host_focused_window_title",
        "macos_test_host_focused_file_path",
        "macos_test_host_focused_file_name",
        "macos_test_host_visible_apps_count",
    }
    # NOTE: the legacy `focused_app_friendly` cleanup publish (which the
    # original screen-time-ha-bridge emitted to remove a renamed entity)
    # is intentionally not part of the merged daemon — fresh installs of
    # macos-mqtt-bridge never published that entity in the first place.


def test_phase_c_publishes_file_path_and_name_for_native_app(
    monkeypatch: pytest.MonkeyPatch, fake_mqtt: MagicMock
):
    """Native document apps populate AXDocument with a file:// URL."""
    _stub_lsappinfo(
        monkeypatch,
        "com.apple.logic10",
        axdocument="file:///Users/testuser/Music/Projects/My%20Song.logicx",
    )
    ticker = _ticker()
    asyncio.run(ticker.run_once(fake_mqtt))

    publishes = {c.args[0]: c.args[1] for c in fake_mqtt.publish_state.call_args_list}
    base = "macos/test_host/focus"
    assert publishes[f"{base}/file_path"] == "/Users/testuser/Music/Projects/My Song.logicx"
    assert publishes[f"{base}/file_name"] == "My Song.logicx"

    attrs = {c.args[0]: c.args[1] for c in fake_mqtt.publish_attributes.call_args_list}
    payload = attrs[f"{base}/attrs"]
    assert payload["file_path"] == "/Users/testuser/Music/Projects/My Song.logicx"
    assert payload["file_name"] == "My Song.logicx"


def test_phase_c_publishes_url_for_browser(
    monkeypatch: pytest.MonkeyPatch, fake_mqtt: MagicMock
):
    """Browsers put the active tab URL in AXDocument."""
    _stub_lsappinfo(
        monkeypatch,
        "com.apple.Safari",
        axdocument="https://news.ycombinator.com/item?id=42",
    )
    ticker = _ticker()
    asyncio.run(ticker.run_once(fake_mqtt))

    publishes = {c.args[0]: c.args[1] for c in fake_mqtt.publish_state.call_args_list}
    base = "macos/test_host/focus"
    assert publishes[f"{base}/file_path"] == "https://news.ycombinator.com/item?id=42"
    assert publishes[f"{base}/file_name"] == "item"


def test_phase_c_empty_file_for_app_without_document(
    monkeypatch: pytest.MonkeyPatch, fake_mqtt: MagicMock
):
    """Apps with no front document (Slack, Terminal, Finder) return ''."""
    _stub_lsappinfo(
        monkeypatch, "com.tinyspeck.slackmacgap", axdocument="",
    )
    ticker = _ticker()
    asyncio.run(ticker.run_once(fake_mqtt))

    publishes = {c.args[0]: c.args[1] for c in fake_mqtt.publish_state.call_args_list}
    base = "macos/test_host/focus"
    assert publishes[f"{base}/file_path"] == ""
    assert publishes[f"{base}/file_name"] == ""


def test_phase_c_file_change_within_same_app_republishes(
    monkeypatch: pytest.MonkeyPatch, fake_mqtt: MagicMock
):
    """Switching files in the same app republishes file_path/file_name even
    if window_title and bundle_id are unchanged."""
    _stub_lsappinfo(
        monkeypatch, "com.apple.logic10",
        window_title="Logic Pro",
        axdocument="file:///Users/testuser/Music/A.logicx",
    )
    ticker = _ticker()
    asyncio.run(ticker.run_once(fake_mqtt))
    fake_mqtt.publish_state.reset_mock()

    _stub_lsappinfo(
        monkeypatch, "com.apple.logic10",
        window_title="Logic Pro",
        axdocument="file:///Users/testuser/Music/B.logicx",
    )
    asyncio.run(ticker.run_once(fake_mqtt))

    topics = {c.args[0]: c.args[1] for c in fake_mqtt.publish_state.call_args_list}
    base = "macos/test_host/focus"
    # Bundle didn't change → focus/app does NOT republish
    assert f"{base}/app" not in topics
    # File path/name should republish
    assert topics[f"{base}/file_path"] == "/Users/testuser/Music/B.logicx"
    assert topics[f"{base}/file_name"] == "B.logicx"


def test_phase_c_split_file_path_and_name_handles_edge_cases():
    from macos_bridge.phases.focused_app import _split_file_path_and_name

    assert _split_file_path_and_name("") == ("", "")
    assert _split_file_path_and_name("file:///tmp/foo.txt") == ("/tmp/foo.txt", "foo.txt")
    # URL-encoded path component
    assert _split_file_path_and_name("file:///tmp/My%20File.txt") == (
        "/tmp/My File.txt", "My File.txt",
    )
    # Bare POSIX path (rare AXDocument shape)
    assert _split_file_path_and_name("/var/log/system.log") == (
        "/var/log/system.log", "system.log",
    )
    # https URL — last path segment is the "name"
    path, name = _split_file_path_and_name("https://example.com/a/b/index.html")
    assert path == "https://example.com/a/b/index.html"
    assert name == "index.html"
    # https URL with no path — fall back to host
    path, name = _split_file_path_and_name("https://example.com/")
    assert path == "https://example.com/"
    assert name == "example.com"
