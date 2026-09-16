"""Tests for the permissions phase ticker.

Probes are stubbed at the module level so the tests don't actually try
to read chat.db or run osascript on the host running pytest.
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest

from macos_bridge.phases import permissions
from macos_bridge.phases.permissions import (
    DENIED,
    GRANTED,
    UNKNOWN,
    PermissionsTicker,
)


@pytest.fixture
def fake_mqtt() -> MagicMock:
    m = MagicMock()
    m.host_slug = "test_host"
    m.sw_version = "0.1.0"
    return m


def _ticker() -> PermissionsTicker:
    return PermissionsTicker(
        enabled=True,
        interval_seconds=300,
        host_slug="test_host",
        topic_prefix="macos",
        availability_topic="macos/test_host/status",
    )


def _state_calls(fake_mqtt: MagicMock) -> dict[str, object]:
    return {c.args[0]: c.args[1] for c in fake_mqtt.publish_state.call_args_list}


def _attr_calls(fake_mqtt: MagicMock) -> dict[str, dict]:
    return {c.args[0]: c.args[1] for c in fake_mqtt.publish_attributes.call_args_list}


def test_state_lists_only_granted_permissions(
    monkeypatch: pytest.MonkeyPatch, fake_mqtt: MagicMock
):
    monkeypatch.setattr(permissions, "_probe_full_disk_access", lambda: GRANTED)
    monkeypatch.setattr(permissions, "_probe_accessibility", lambda: GRANTED)
    monkeypatch.setattr(permissions, "_probe_automation_messages", lambda: DENIED)
    monkeypatch.setattr(permissions, "_probe_location_services", lambda *a, **kw: UNKNOWN)

    asyncio.run(_ticker().run_once(fake_mqtt))

    state = _state_calls(fake_mqtt)["macos/test_host/permissions/granted"]
    assert state == "Full Disk Access, Accessibility"


def test_state_is_none_string_when_nothing_granted(
    monkeypatch: pytest.MonkeyPatch, fake_mqtt: MagicMock
):
    monkeypatch.setattr(permissions, "_probe_full_disk_access", lambda: DENIED)
    monkeypatch.setattr(permissions, "_probe_accessibility", lambda: DENIED)
    monkeypatch.setattr(permissions, "_probe_automation_messages", lambda: DENIED)
    monkeypatch.setattr(permissions, "_probe_location_services", lambda *a, **kw: DENIED)

    asyncio.run(_ticker().run_once(fake_mqtt))
    assert _state_calls(fake_mqtt)["macos/test_host/permissions/granted"] == "None"


def test_attrs_carry_per_permission_status_and_metadata(
    monkeypatch: pytest.MonkeyPatch, fake_mqtt: MagicMock
):
    monkeypatch.setattr(permissions, "_probe_full_disk_access", lambda: GRANTED)
    monkeypatch.setattr(permissions, "_probe_accessibility", lambda: DENIED)
    monkeypatch.setattr(permissions, "_probe_automation_messages", lambda: UNKNOWN)
    monkeypatch.setattr(permissions, "_probe_location_services", lambda *a, **kw: GRANTED)

    asyncio.run(_ticker().run_once(fake_mqtt))

    attrs = _attr_calls(fake_mqtt)["macos/test_host/permissions/granted/attrs"]
    assert attrs["full_disk_access"] == GRANTED
    assert attrs["accessibility"] == DENIED
    assert attrs["automation_messages"] == UNKNOWN
    assert attrs["location_services"] == GRANTED
    assert attrs["granted_count"] == 2
    assert attrs["granted"] == ["Full Disk Access", "Location Services"]
    assert "checked_at" in attrs
    assert attrs["python_path"].endswith("python") or attrs["python_path"].endswith(
        "python3"
    ) or "python" in attrs["python_path"]


def test_publishes_two_discovery_entities(fake_mqtt: MagicMock):
    """One sensor for granted permissions, one symmetrical sensor for
    denied — both tagged ``entity_category=diagnostic``."""
    asyncio.run(_ticker().publish_discovery(fake_mqtt))
    assert fake_mqtt.publish_discovery.call_count == 2
    by_uid = {
        c.kwargs["unique_id"]: c.kwargs["payload"]
        for c in fake_mqtt.publish_discovery.call_args_list
    }
    assert "macos_test_host_permissions_granted" in by_uid
    assert "macos_test_host_permissions_denied" in by_uid

    granted = by_uid["macos_test_host_permissions_granted"]
    assert granted["name"] == "macOS Permissions - Granted"
    assert granted["json_attributes_topic"] == "macos/test_host/permissions/granted/attrs"
    assert granted["entity_category"] == "diagnostic"

    denied = by_uid["macos_test_host_permissions_denied"]
    assert denied["name"] == "macOS Permissions - Denied"
    assert denied["json_attributes_topic"] == "macos/test_host/permissions/denied/attrs"
    assert denied["entity_category"] == "diagnostic"


def test_denied_state_lists_only_denied_permissions(
    monkeypatch: pytest.MonkeyPatch, fake_mqtt: MagicMock
):
    """The Denied sensor mirrors the Granted shape: comma-joined list of
    just the denied labels, with attrs that include both granted+denied
    counts so HA templates can read either side."""
    monkeypatch.setattr(permissions, "_probe_full_disk_access", lambda: GRANTED)
    monkeypatch.setattr(permissions, "_probe_accessibility", lambda: DENIED)
    monkeypatch.setattr(permissions, "_probe_automation_messages", lambda: UNKNOWN)
    monkeypatch.setattr(permissions, "_probe_location_services", lambda *a, **kw: DENIED)

    asyncio.run(_ticker().run_once(fake_mqtt))

    state = _state_calls(fake_mqtt)["macos/test_host/permissions/denied"]
    assert state == "Accessibility, Location Services"

    attrs = _attr_calls(fake_mqtt)["macos/test_host/permissions/denied/attrs"]
    assert attrs["denied"] == ["Accessibility", "Location Services"]
    assert attrs["denied_count"] == 2
    assert attrs["granted"] == ["Full Disk Access"]
    assert attrs["granted_count"] == 1


def test_denied_state_is_none_when_nothing_denied(
    monkeypatch: pytest.MonkeyPatch, fake_mqtt: MagicMock
):
    """When no permission is in the explicit DENIED state (all granted or
    unknown), Denied sensor reads "None" rather than empty string."""
    monkeypatch.setattr(permissions, "_probe_full_disk_access", lambda: GRANTED)
    monkeypatch.setattr(permissions, "_probe_accessibility", lambda: GRANTED)
    monkeypatch.setattr(permissions, "_probe_automation_messages", lambda: UNKNOWN)
    monkeypatch.setattr(permissions, "_probe_location_services", lambda *a, **kw: GRANTED)

    asyncio.run(_ticker().run_once(fake_mqtt))
    assert _state_calls(fake_mqtt)["macos/test_host/permissions/denied"] == "None"


# ---- _probe_location_services: helper-based probe ---------------------------


def test_probe_location_returns_unknown_when_binary_path_not_provided():
    """No binary_path → UNKNOWN. We refuse to guess from the daemon's
    own grant (which is irrelevant) or from system_profiler heuristics
    (which historically gave false-denied reports)."""
    assert permissions._probe_location_services(None) == UNKNOWN
    assert permissions._probe_location_services("") == UNKNOWN


def test_probe_location_returns_unknown_when_binary_missing(tmp_path):
    """Binary path that doesn't exist or isn't executable → UNKNOWN."""
    nonexistent = tmp_path / "does-not-exist"
    assert permissions._probe_location_services(str(nonexistent)) == UNKNOWN

    not_executable = tmp_path / "not-exec"
    not_executable.write_text("")
    assert permissions._probe_location_services(str(not_executable)) == UNKNOWN


def _fake_run_factory(stdout: str, returncode: int = 0):
    """Build a subprocess.run replacement that returns the given stdout."""
    def _fake(*args, **kwargs):
        result = MagicMock()
        result.stdout = stdout
        result.stderr = ""
        result.returncode = returncode
        return result
    return _fake


def test_probe_location_translates_helper_authorization_values(
    monkeypatch: pytest.MonkeyPatch, tmp_path
):
    """The helper emits {"authorization": "<status>"} — translate each
    status to the corresponding GRANTED/DENIED/UNKNOWN."""
    binary = tmp_path / "LocationFetcher"
    binary.write_text("")
    binary.chmod(0o755)
    # tmp_path can live on a noexec mount (some Linux hosts mount /tmp that way), where
    # os.access(..., X_OK) returns False regardless of the mode bits and
    # the probe would short-circuit to UNKNOWN before parsing. The
    # executable-bit guard has its own dedicated test; here we only
    # exercise the JSON-translation branch, so force the guard to pass.
    monkeypatch.setattr(permissions.os, "access", lambda *a, **k: True)

    cases = {
        '{"authorization": "granted"}': GRANTED,
        '{"authorization": "denied"}': DENIED,
        '{"authorization": "restricted"}': DENIED,
        '{"authorization": "not_determined"}': UNKNOWN,
        '{"authorization": "unknown"}': UNKNOWN,
        '{"authorization": "something_new_we_havent_seen"}': UNKNOWN,
    }
    for stdout, expected in cases.items():
        monkeypatch.setattr(
            permissions.subprocess, "run", _fake_run_factory(stdout)
        )
        assert permissions._probe_location_services(str(binary)) == expected, stdout


def test_probe_location_returns_unknown_on_malformed_helper_output(
    monkeypatch: pytest.MonkeyPatch, tmp_path
):
    """Garbage stdout / empty stdout / missing authorization key →
    UNKNOWN, not DENIED. Honest about ambiguity beats a misleading
    false-positive."""
    binary = tmp_path / "LocationFetcher"
    binary.write_text("")
    binary.chmod(0o755)

    for stdout in ("", "not json at all", '{"unrelated_key": 1}'):
        monkeypatch.setattr(
            permissions.subprocess, "run", _fake_run_factory(stdout)
        )
        assert permissions._probe_location_services(str(binary)) == UNKNOWN


def test_probe_location_returns_unknown_on_helper_subprocess_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path
):
    """TimeoutExpired / OSError from the helper → UNKNOWN."""
    import subprocess as _sub

    binary = tmp_path / "LocationFetcher"
    binary.write_text("")
    binary.chmod(0o755)

    def _raise_timeout(*a, **kw):
        raise _sub.TimeoutExpired(cmd="LocationFetcher", timeout=5)
    monkeypatch.setattr(permissions.subprocess, "run", _raise_timeout)
    assert permissions._probe_location_services(str(binary)) == UNKNOWN

    def _raise_oserror(*a, **kw):
        raise OSError("spawn failed")
    monkeypatch.setattr(permissions.subprocess, "run", _raise_oserror)
    assert permissions._probe_location_services(str(binary)) == UNKNOWN


def test_ticker_passes_location_binary_path_through_to_probe(
    monkeypatch: pytest.MonkeyPatch, fake_mqtt: MagicMock, tmp_path
):
    """PermissionsTicker must forward its ``location_binary_path`` into
    _probe_all so the helper's grant is the one being reported."""
    binary = tmp_path / "LocationFetcher"
    binary.write_text("")
    binary.chmod(0o755)
    seen_paths: list[str | None] = []

    def _fake_probe(binary_path=None, timeout=5.0):
        seen_paths.append(binary_path)
        return GRANTED

    monkeypatch.setattr(permissions, "_probe_full_disk_access", lambda: GRANTED)
    monkeypatch.setattr(permissions, "_probe_accessibility", lambda: GRANTED)
    monkeypatch.setattr(permissions, "_probe_automation_messages", lambda: GRANTED)
    monkeypatch.setattr(permissions, "_probe_location_services", _fake_probe)

    ticker = PermissionsTicker(
        enabled=True,
        interval_seconds=300,
        host_slug="test_host",
        topic_prefix="macos",
        availability_topic="macos/test_host/status",
        location_binary_path=str(binary),
    )
    asyncio.run(ticker.run_once(fake_mqtt))

    assert seen_paths == [str(binary)]
    attrs = _attr_calls(fake_mqtt)["macos/test_host/permissions/granted/attrs"]
    assert attrs["location_binary_path"] == str(binary)
