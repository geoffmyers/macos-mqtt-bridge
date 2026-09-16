"""Probe and report the macOS TCC permissions the bridge currently holds.

The bridge depends on a handful of system permissions that have to be
granted to the venv's python binary in System Settings → Privacy &
Security — with one exception (Location), which is granted to a
separate Swift helper bundle instead of the daemon itself:

  - Full Disk Access: required to read chat.db, CallHistory.storedata,
    the FaceTime voicemail store, knowledgeC.db, RMAdminStore-Local.sqlite,
    and the AddressBook source DBs. Granted to .venv/bin/python.
  - Accessibility: required for window-title / AXDocument extraction in
    the focused_app phase. Granted to .venv/bin/python.
  - Automation (Messages): required for outbound iMessage/SMS sending
    via osascript against Messages.app's scripting dictionary. Granted
    to .venv/bin/python.
  - Location Services: granted to the LocationFetcher.app helper at
    helpers/location-fetcher/.build/release/LocationFetcher.app rather
    than to the daemon. The Python daemon never holds this grant — the
    architecture intentionally delegates CoreLocation to a stable signed
    Swift bundle so the grant survives venv rebuilds. The probe in this
    module therefore invokes LocationFetcher with --check-authorization
    and reports *the helper's* TCC status, not the daemon's. Reporting
    the daemon's status would always be "denied" even when location data
    is flowing fine, which is the opposite of useful.

Publishes one HA sensor whose state is a comma-joined list of granted
permission names, plus json_attributes_topic carrying per-permission
status (``granted`` | ``denied`` | ``unknown``) so HA automations and
templates can key off individual permissions.

Runs on a slow cadence (default 5 min) — these don't change unless the
user toggles them in System Settings.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from macos_bridge.mqtt import MqttPublisher, build_discovery_payload
from macos_bridge.phases.base import (
    AbstractTicker,
    screen_time_state_topic,
    screen_time_unique_id,
)

logger = logging.getLogger(__name__)

# Per-permission status values.
GRANTED = "granted"
DENIED = "denied"
UNKNOWN = "unknown"

# (key, friendly_name)
_PERMISSION_LABELS = (
    ("full_disk_access", "Full Disk Access"),
    ("accessibility", "Accessibility"),
    ("automation_messages", "Automation (Messages)"),
    ("location_services", "Location Services"),
)


def _probe_full_disk_access() -> str:
    """FDA = the calling process can open TCC-protected user-data files.
    Use chat.db as the canary because the bridge needs it anyway and
    it's universally present on a logged-in macOS account.
    """
    chat_db = Path("~/Library/Messages/chat.db").expanduser()
    if not chat_db.exists():
        return UNKNOWN
    try:
        with open(chat_db, "rb") as f:
            f.read(1)
        return GRANTED
    except PermissionError:
        return DENIED
    except OSError as exc:
        logger.debug("FDA probe OSError: %s", exc)
        return UNKNOWN


def _probe_accessibility(timeout: float = 3.0) -> str:
    """Accessibility = process can use System Events to read UI state.
    Probe with a benign read-only query that returns the name of any
    process. AppleScript error -1743 (or message containing 'not
    authorized') signals denial.
    """
    try:
        result = subprocess.run(
            ["osascript", "-e",
             'tell application "System Events" to get name of first process'],
            capture_output=True, text=True, timeout=timeout, check=False,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return UNKNOWN
    if result.returncode == 0 and result.stdout.strip():
        return GRANTED
    err = (result.stderr or "").lower()
    if "1743" in err or "not authorized" in err or "not allowed" in err:
        return DENIED
    return UNKNOWN


def _is_messages_running(timeout: float = 2.0) -> bool:
    """Return True iff Messages.app currently has a process — used to
    decide whether the Automation probe will give a clean answer or
    just launch the app (which we don't want)."""
    try:
        result = subprocess.run(
            ["osascript", "-e",
             'tell application "System Events" to (name of processes contains "Messages")'],
            capture_output=True, text=True, timeout=timeout, check=False,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return False
    return result.returncode == 0 and result.stdout.strip().lower() == "true"


def _probe_automation_messages(timeout: float = 3.0) -> str:
    """Automation (Messages) = calling process can send AppleScript
    events to Messages.app. Probe by reading something benign from the
    Messages dictionary, but only if the app is already running — so we
    don't launch Messages just to test permission. If Messages isn't
    running we report ``unknown`` rather than guess.
    """
    if not _is_messages_running():
        return UNKNOWN
    try:
        result = subprocess.run(
            ["osascript", "-e",
             'tell application "Messages" to count of accounts'],
            capture_output=True, text=True, timeout=timeout, check=False,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return UNKNOWN
    if result.returncode == 0:
        return GRANTED
    err = (result.stderr or "").lower()
    if "1743" in err or "not authorized" in err or "not allowed" in err:
        return DENIED
    return UNKNOWN


def _probe_location_services(
    binary_path: str | None = None, timeout: float = 5.0
) -> str:
    """Location Services = the LocationFetcher.app helper holds the TCC
    grant, not the Python daemon. Invoke the helper with
    ``--check-authorization`` (passive read of
    ``CLLocationManager.authorizationStatus`` — does not prompt, does
    not start an update) and translate its JSON output:

      ``granted``           → GRANTED
      ``denied`` /
      ``restricted``        → DENIED
      ``not_determined``    → UNKNOWN (user hasn't decided yet — a
                              prompt will appear the first time the
                              helper actually tries to fetch)
      anything else         → UNKNOWN

    If ``binary_path`` is None, the file doesn't exist, or the helper
    can't be invoked, we return UNKNOWN rather than guessing — the
    previous heuristic (``system_profiler SPAirPortDataType`` looking
    for ``<redacted>`` SSIDs) reported the daemon's grant, not the
    helper's, which produced false "denied" reports whenever location
    data was actually flowing through the helper subprocess. UNKNOWN
    is the honest answer when we can't ask the right binary.
    """
    if not binary_path:
        return UNKNOWN
    expanded = os.path.expanduser(binary_path)
    if not os.path.isfile(expanded) or not os.access(expanded, os.X_OK):
        return UNKNOWN
    try:
        result = subprocess.run(
            [expanded, "--check-authorization"],
            capture_output=True, text=True, timeout=timeout, check=False,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return UNKNOWN
    stdout = (result.stdout or "").strip()
    if not stdout:
        return UNKNOWN
    # Helper emits one JSON object per line; the auth payload is on the
    # last line (matches the fetch-mode convention).
    last_line = stdout.splitlines()[-1]
    try:
        payload = json.loads(last_line)
    except json.JSONDecodeError:
        return UNKNOWN
    auth = payload.get("authorization")
    if auth == "granted":
        return GRANTED
    if auth in ("denied", "restricted"):
        return DENIED
    return UNKNOWN


def _probe_all(location_binary_path: str | None = None) -> dict[str, str]:
    return {
        "full_disk_access": _probe_full_disk_access(),
        "accessibility": _probe_accessibility(),
        "automation_messages": _probe_automation_messages(),
        "location_services": _probe_location_services(location_binary_path),
    }


def _granted_label_list(status: dict[str, str]) -> list[str]:
    return [
        friendly
        for key, friendly in _PERMISSION_LABELS
        if status.get(key) == GRANTED
    ]


def _denied_label_list(status: dict[str, str]) -> list[str]:
    """Return the friendly names of permissions whose status is exactly
    DENIED (not UNKNOWN). Symmetric counterpart to ``_granted_label_list``."""
    return [
        friendly
        for key, friendly in _PERMISSION_LABELS
        if status.get(key) == DENIED
    ]


class PermissionsTicker(AbstractTicker):
    name = "permissions"

    def __init__(
        self,
        *,
        enabled: bool,
        interval_seconds: int,
        host_slug: str,
        topic_prefix: str,
        availability_topic: str,
        location_binary_path: str | None = None,
    ) -> None:
        self._enabled = enabled
        self._interval_seconds = interval_seconds
        self._host_slug = host_slug
        self._topic_prefix = topic_prefix
        self._availability = availability_topic
        # Path to the LocationFetcher.app helper binary. Used by the
        # location_services probe to query the *helper's* TCC grant
        # rather than the daemon's (which never holds the grant). When
        # None, the probe returns UNKNOWN.
        self._location_binary_path = (
            os.path.expanduser(location_binary_path)
            if location_binary_path
            else None
        )

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def interval_seconds(self) -> int | None:
        return self._interval_seconds

    def _granted_state_topic(self) -> str:
        return screen_time_state_topic(
            self._topic_prefix, self._host_slug, "permissions/granted"
        )

    def _granted_attrs_topic(self) -> str:
        return screen_time_state_topic(
            self._topic_prefix, self._host_slug, "permissions/granted/attrs"
        )

    def _denied_state_topic(self) -> str:
        return screen_time_state_topic(
            self._topic_prefix, self._host_slug, "permissions/denied"
        )

    def _denied_attrs_topic(self) -> str:
        return screen_time_state_topic(
            self._topic_prefix, self._host_slug, "permissions/denied/attrs"
        )

    def _granted_unique_id(self) -> str:
        return screen_time_unique_id(self._host_slug, "permissions_granted")

    def _denied_unique_id(self) -> str:
        return screen_time_unique_id(self._host_slug, "permissions_denied")

    # Back-compat aliases — older callers / tests reference these.
    _state_topic = _granted_state_topic
    _attrs_topic = _granted_attrs_topic
    _unique_id = _granted_unique_id

    async def run_once(self, mqtt: MqttPublisher) -> None:
        status = _probe_all(self._location_binary_path)
        granted = _granted_label_list(status)
        denied = _denied_label_list(status)
        granted_state = ", ".join(granted) if granted else "None"
        denied_state = ", ".join(denied) if denied else "None"

        # Granted attrs hold the full per-permission status map so a single
        # `state_attr()` template can read all four flags. Denied attrs
        # mirror just the denied subset for symmetry.
        granted_attrs: dict[str, Any] = dict(status)
        granted_attrs["granted_count"] = len(granted)
        granted_attrs["granted"] = granted
        granted_attrs["denied_count"] = len(denied)
        granted_attrs["denied"] = denied
        granted_attrs["checked_at"] = datetime.now(tz=UTC).isoformat()
        # The TCC grant attaches to whichever binary actually ran the
        # check; surfacing it makes it obvious in HA which executable
        # the user needs to add/re-add to the System Settings list when
        # something is denied. FDA/Accessibility/Automation are keyed on
        # the venv python; Location is keyed on the LocationFetcher.app
        # helper bundle (a separate signed binary).
        granted_attrs["python_path"] = sys.executable
        granted_attrs["location_binary_path"] = self._location_binary_path

        denied_attrs: dict[str, Any] = {
            "denied_count": len(denied),
            "denied": denied,
            "granted_count": len(granted),
            "granted": granted,
            "checked_at": granted_attrs["checked_at"],
            "python_path": sys.executable,
            "location_binary_path": self._location_binary_path,
            **status,
        }

        mqtt.publish_state(self._granted_state_topic(), granted_state)
        mqtt.publish_attributes(self._granted_attrs_topic(), granted_attrs)
        mqtt.publish_state(self._denied_state_topic(), denied_state)
        mqtt.publish_attributes(self._denied_attrs_topic(), denied_attrs)

        logger.info(
            "permissions tick: granted=%d/%d denied=%d — fda=%s acc=%s auto_msgs=%s loc=%s",
            len(granted), len(_PERMISSION_LABELS), len(denied),
            status["full_disk_access"], status["accessibility"],
            status["automation_messages"], status["location_services"],
        )

    async def publish_discovery(self, mqtt: MqttPublisher) -> None:
        device = mqtt.host_device_block()
        mqtt.publish_discovery(
            component="sensor",
            unique_id=self._granted_unique_id(),
            payload=build_discovery_payload(
                name="macOS Permissions - Granted",
                unique_id=self._granted_unique_id(),
                state_topic=self._granted_state_topic(),
                availability_topic=None,
                device=device,
                icon="mdi:shield-check",
                entity_category="diagnostic",
                json_attributes_topic=self._granted_attrs_topic(),
            ),
        )
        mqtt.publish_discovery(
            component="sensor",
            unique_id=self._denied_unique_id(),
            payload=build_discovery_payload(
                name="macOS Permissions - Denied",
                unique_id=self._denied_unique_id(),
                state_topic=self._denied_state_topic(),
                availability_topic=None,
                device=device,
                icon="mdi:shield-alert",
                entity_category="diagnostic",
                json_attributes_topic=self._denied_attrs_topic(),
            ),
        )
