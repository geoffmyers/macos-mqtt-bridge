"""Tests for Phase F system-state ticker."""

from __future__ import annotations

import asyncio
import subprocess
from unittest.mock import MagicMock

import pytest

from macos_bridge.phases.system_state import SystemStateTicker


@pytest.fixture
def fake_mqtt() -> MagicMock:
    m = MagicMock()
    m.host_slug = "test_host"
    m.sw_version = "0.1.0"
    m.host_friendly_name = "Test Host"
    return m


def _ticker() -> SystemStateTicker:
    return SystemStateTicker(
        enabled=True,
        interval_seconds=30,
        host_slug="test_host",
        topic_prefix="macos",
        availability_topic="macos/test_host/status",
        wifi_interface="en0",
    )


def _stub(monkeypatch, **outputs):
    """Stub subprocess.run; outputs maps a tool name to (rc, stdout).

    `system_profiler` is invoked twice with different arguments; the stub
    differentiates them via dedicated keys:
      - `__sp_airport`     → SPAirPortDataType (Wi-Fi SSID)
      - `__sp_bluetooth`   → SPBluetoothDataType (BT connected list)
    The legacy `system_profiler` key is kept as a fallback for tests that
    don't care which datatype is queried.
    """

    def fake_run(args, *a, **kw):
        first = args[0]
        if first == "system_profiler":
            datatype = args[1] if len(args) > 1 else ""
            if "AirPort" in datatype:
                rc, stdout = outputs.get(
                    "__sp_airport", outputs.get("system_profiler", (0, ""))
                )
                return subprocess.CompletedProcess(args, rc, stdout=stdout, stderr="")
            if "Bluetooth" in datatype:
                rc, stdout = outputs.get(
                    "__sp_bluetooth", outputs.get("system_profiler", (0, ""))
                )
                return subprocess.CompletedProcess(args, rc, stdout=stdout, stderr="")
        if first in outputs:
            rc, stdout = outputs[first]
            return subprocess.CompletedProcess(args, rc, stdout=stdout, stderr="")
        # Pass-through for osascript: distinguish on arg
        if first == "osascript":
            script = args[2] if len(args) > 2 else ""
            if "muted" in script:
                rc, stdout = outputs.get("__osascript_muted", (0, "false"))
                return subprocess.CompletedProcess(args, rc, stdout=stdout, stderr="")
            rc, stdout = outputs.get("__osascript_volume", (0, "50"))
            return subprocess.CompletedProcess(args, rc, stdout=stdout, stderr="")
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)


_AIRPORT_CONNECTED = (
    "Wi-Fi:\n"
    "      Software Versions:\n"
    "        ...\n"
    "      Interfaces:\n"
    "        en0:\n"
    "          Card Type: Wi-Fi (0x14E4, 0x16D)\n"
    "          Status: Connected\n"
    "          Current Network Information:\n"
    "            Example Home 5G:\n"
    "              PHY Mode: 802.11ax\n"
    "              Channel: 100 (5GHz, 80MHz)\n"
    "              Network Type: Infrastructure\n"
    "          Other Local Wi-Fi Networks:\n"
    "            SomeNeighbor:\n"
    "              Channel: 6\n"
)

_AIRPORT_DISCONNECTED = (
    "Wi-Fi:\n"
    "      Interfaces:\n"
    "        en0:\n"
    "          Status: Off\n"
)

# What system_profiler returns when the calling binary doesn't hold the
# Location Services TCC grant — Apple substitutes "<redacted>" for the
# real SSID. The bridge replaces this with a self-explanatory string
# rather than passing the inscrutable token through to HA.
_AIRPORT_REDACTED = (
    "Wi-Fi:\n"
    "      Interfaces:\n"
    "        en0:\n"
    "          Status: Connected\n"
    "          Current Network Information:\n"
    "            <redacted>:\n"
    "              PHY Mode: 802.11ax\n"
    "              Channel: 100 (5GHz, 80MHz)\n"
)


def test_phase_f_publishes_wifi_ssid(monkeypatch, fake_mqtt: MagicMock):
    _stub(
        monkeypatch,
        __sp_airport=(0, _AIRPORT_CONNECTED),
        __sp_bluetooth=(0, ""),
        ioreg=(0, ""),
        pmset=(0, ""),
    )
    asyncio.run(_ticker().run_once(fake_mqtt))
    p = {c.args[0]: c.args[1] for c in fake_mqtt.publish_state.call_args_list}
    assert p["macos/test_host/system/wifi_ssid"] == "Example Home 5G"


def test_phase_f_wifi_ssid_empty_when_not_associated(monkeypatch, fake_mqtt: MagicMock):
    _stub(
        monkeypatch,
        __sp_airport=(0, _AIRPORT_DISCONNECTED),
        __sp_bluetooth=(0, ""),
        ioreg=(0, ""),
        pmset=(0, ""),
    )
    asyncio.run(_ticker().run_once(fake_mqtt))
    p = {c.args[0]: c.args[1] for c in fake_mqtt.publish_state.call_args_list}
    assert p["macos/test_host/system/wifi_ssid"] == ""


def test_phase_f_wifi_ssid_replaces_redacted_with_actionable_label(
    monkeypatch, fake_mqtt: MagicMock
):
    """When system_profiler redacts the SSID due to missing Location Services
    grant, the parser must surface a self-explanatory string to HA rather
    than the inscrutable "<redacted>" token."""
    _stub(
        monkeypatch,
        __sp_airport=(0, _AIRPORT_REDACTED),
        __sp_bluetooth=(0, ""),
        ioreg=(0, ""),
        pmset=(0, ""),
    )
    # Reset the warn-throttle so the test always exercises the warn path,
    # regardless of whether a previous test in this run already tripped it.
    from macos_bridge.phases import system_state
    system_state._last_location_warn_at = None

    asyncio.run(_ticker().run_once(fake_mqtt))
    p = {c.args[0]: c.args[1] for c in fake_mqtt.publish_state.call_args_list}
    assert p["macos/test_host/system/wifi_ssid"] == "(location services denied)"


def test_phase_f_wifi_ssid_handles_colons_in_name(monkeypatch, fake_mqtt: MagicMock):
    """SSID `Fatal Error: Wi-Fi Not Found 5G` contains its own colons —
    only the trailing block-header colon should be stripped."""
    weird = (
        "          Current Network Information:\n"
        "            Fatal Error: Wi-Fi Not Found 5G:\n"
        "              PHY Mode: 802.11ax\n"
    )
    _stub(
        monkeypatch,
        __sp_airport=(0, weird),
        __sp_bluetooth=(0, ""),
        ioreg=(0, ""),
        pmset=(0, ""),
    )
    asyncio.run(_ticker().run_once(fake_mqtt))
    p = {c.args[0]: c.args[1] for c in fake_mqtt.publish_state.call_args_list}
    assert p["macos/test_host/system/wifi_ssid"] == (
        "Fatal Error: Wi-Fi Not Found 5G"
    )


def test_phase_f_wifi_skips_publish_on_read_failure(monkeypatch, fake_mqtt: MagicMock):
    """When system_profiler fails / times out, _wifi_ssid returns None and
    run_once must NOT publish wifi_ssid (preserving last retained value)."""
    _stub(
        monkeypatch,
        __sp_airport=(1, ""),  # non-zero rc → _run returns None → ssid is None
        __sp_bluetooth=(0, ""),
        ioreg=(0, ""),
        pmset=(0, ""),
    )
    asyncio.run(_ticker().run_once(fake_mqtt))
    state_topics = [c.args[0] for c in fake_mqtt.publish_state.call_args_list]
    # Other Phase F sensors still publish; wifi_ssid is intentionally absent
    assert "macos/test_host/system/wifi_ssid" not in state_topics
    assert "macos/test_host/system/bluetooth_connected_count" in state_topics


def test_phase_f_bluetooth_count_and_names(monkeypatch, fake_mqtt: MagicMock):
    bt_out = (
        "Bluetooth:\n"
        "    Connected:\n"
        "        AirPods Pro:\n"
        "          MAC: aa:bb\n"
        "        Magic Mouse:\n"
        "          MAC: cc:dd\n"
        "    Not Connected:\n"
        "        Old Keyboard:\n"
    )
    _stub(
        monkeypatch,
        networksetup=(0, ""), system_profiler=(0, bt_out),
        ioreg=(0, ""), pmset=(0, ""),
    )
    asyncio.run(_ticker().run_once(fake_mqtt))
    p = {c.args[0]: c.args[1] for c in fake_mqtt.publish_state.call_args_list}
    assert p["macos/test_host/system/bluetooth_connected_count"] == 2

    attrs = {c.args[0]: c.args[1] for c in fake_mqtt.publish_attributes.call_args_list}
    assert "AirPods Pro" in attrs[
        "macos/test_host/system/bluetooth_connected_count/attrs"
    ]["connected_devices"]


def _ioreg_brightness(value: int, max_val: int = 65536) -> str:
    return (
        f'      "IODisplayParameters" = {{"BrightnessMilliNits"={{"min"=3979,'
        f'"value"=300000,"uncalMilliNits"=140000,"max"=1599999}},'
        f'"brightness"={{"min"=0,"max"={max_val},"value"={value}}},'
        f'"rawBrightness"={{"min"=0,"max"=2047,"value"=1500}},'
        f'"BrightnessMicroAmps"={{"min"=51,"max"=13799,"value"=3199}}}}\n'
    )


def test_phase_f_screen_brightness_user_100_slider_returns_100(
    monkeypatch, fake_mqtt: MagicMock
):
    """On Apple Silicon XDR displays, value=max/2 corresponds to the
    user's 100% slider — the upper half is HDR boost. The bridge should
    report 100%, not 50%, in that case."""
    _stub(
        monkeypatch,
        __sp_airport=(0, ""), __sp_bluetooth=(0, ""),
        ioreg=(0, _ioreg_brightness(value=32768, max_val=65536)),
        pmset=(0, ""),
    )
    asyncio.run(_ticker().run_once(fake_mqtt))
    p = {c.args[0]: c.args[1] for c in fake_mqtt.publish_state.call_args_list}
    assert p["macos/test_host/system/screen_brightness"] == 100.0


def test_phase_f_screen_brightness_user_50_slider(monkeypatch, fake_mqtt: MagicMock):
    """At slider 50%, value should be ~max/4 → 50% reported."""
    _stub(
        monkeypatch,
        __sp_airport=(0, ""), __sp_bluetooth=(0, ""),
        ioreg=(0, _ioreg_brightness(value=16384, max_val=65536)),
        pmset=(0, ""),
    )
    asyncio.run(_ticker().run_once(fake_mqtt))
    p = {c.args[0]: c.args[1] for c in fake_mqtt.publish_state.call_args_list}
    assert p["macos/test_host/system/screen_brightness"] == 50.0


def test_phase_f_screen_brightness_hdr_boost_clipped_at_100(
    monkeypatch, fake_mqtt: MagicMock
):
    """If HDR content drives value above max/2, the user-perceived
    brightness is still 100% — clip rather than reporting >100%."""
    _stub(
        monkeypatch,
        __sp_airport=(0, ""), __sp_bluetooth=(0, ""),
        ioreg=(0, _ioreg_brightness(value=49152, max_val=65536)),
        pmset=(0, ""),
    )
    asyncio.run(_ticker().run_once(fake_mqtt))
    p = {c.args[0]: c.args[1] for c in fake_mqtt.publish_state.call_args_list}
    assert p["macos/test_host/system/screen_brightness"] == 100.0


def test_phase_f_volume_and_muted(monkeypatch, fake_mqtt: MagicMock):
    _stub(
        monkeypatch,
        networksetup=(0, ""), system_profiler=(0, ""),
        ioreg=(0, ""), pmset=(0, ""),
        __osascript_volume=(0, "65"),
        __osascript_muted=(0, "true"),
    )
    asyncio.run(_ticker().run_once(fake_mqtt))
    p = {c.args[0]: c.args[1] for c in fake_mqtt.publish_state.call_args_list}
    assert p["macos/test_host/system/volume_level"] == 65
    assert p["macos/test_host/system/volume_muted"] == "ON"


def test_phase_f_lid_open_vs_closed(monkeypatch, fake_mqtt: MagicMock):
    _stub(
        monkeypatch,
        networksetup=(0, ""), system_profiler=(0, ""),
        ioreg=(0, '  |   "AppleClamshellState" = Yes\n'),
        pmset=(0, ""),
    )
    asyncio.run(_ticker().run_once(fake_mqtt))
    p = {c.args[0]: c.args[1] for c in fake_mqtt.publish_state.call_args_list}
    assert p["macos/test_host/system/lid_closed"] == "ON"


def test_phase_f_battery_percent_and_charging(monkeypatch, fake_mqtt: MagicMock):
    pmset_out = (
        "Now drawing from 'AC Power'\n"
        " -InternalBattery-0 (id=14745699)\t82%; charging; 0:42 remaining present: true\n"
    )
    _stub(
        monkeypatch,
        networksetup=(0, ""), system_profiler=(0, ""),
        ioreg=(0, ""), pmset=(0, pmset_out),
    )
    asyncio.run(_ticker().run_once(fake_mqtt))
    p = {c.args[0]: c.args[1] for c in fake_mqtt.publish_state.call_args_list}
    assert p["macos/test_host/system/battery_percent"] == 82
    assert p["macos/test_host/system/battery_charging"] == "ON"


def test_phase_f_publishes_nine_discovery_configs(fake_mqtt: MagicMock):
    asyncio.run(_ticker().publish_discovery(fake_mqtt))
    assert fake_mqtt.publish_discovery.call_count == 9
    components = [c.kwargs["component"] for c in fake_mqtt.publish_discovery.call_args_list]
    assert components.count("sensor") == 5  # ssid, bt count, brightness, volume, battery
    # muted, lid, charging, screensaver_running
    assert components.count("binary_sensor") == 4
