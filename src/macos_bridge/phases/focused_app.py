"""Phase C: live currently-focused app via macOS `lsappinfo`.

Modern macOS (>= ~12) writes `/app/usage` rows to knowledgeC.db only
*after* focus leaves an app. So knowledgeC can't tell you the
currently-focused app — only which app last lost focus.

Instead, this ticker shells out to `lsappinfo` (built-in macOS tool that
reads window state from the WindowServer) on a `poll_interval_seconds`
cadence. lsappinfo doesn't require TCC Accessibility permission; FDA
isn't needed either.

When the screen is locked, lsappinfo reports `com.apple.loginwindow` as
the frontmost.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import subprocess
import time
from datetime import UTC, datetime
from urllib.parse import unquote, urlparse

from macos_bridge.apps import bundle_id_to_app_name
from macos_bridge.mqtt import MqttPublisher, build_discovery_payload
from macos_bridge.phases.base import (
    AbstractTicker,
    screen_time_state_topic,
    screen_time_unique_id,
)

logger = logging.getLogger(__name__)

_LOCKED_BUNDLE = "com.apple.loginwindow"
_FRIENDLY_OVERRIDES = {
    "com.apple.loginwindow": "Login Window",
}
_BUNDLE_ID_RE = re.compile(r'(?:bundleID|"CFBundleIdentifier")="([^"]+)"')
_VERSION_RE = re.compile(r'Version="([^"]+)"')
_ARCH_RE = re.compile(r"Arch=(\S+)")
_BUNDLE_PATH_RE = re.compile(r'bundle path="([^"]+)"')
_CHECKIN_RE = re.compile(r"checkin time = (\d{4}/\d{2}/\d{2} \d{2}:\d{2}:\d{2})")


def _resolve_friendly(bundle_id: str | None) -> str:
    if not bundle_id:
        return "None"
    if bundle_id in _FRIENDLY_OVERRIDES:
        return _FRIENDLY_OVERRIDES[bundle_id]
    return bundle_id_to_app_name(bundle_id)


def _frontmost_asn() -> str | None:
    try:
        result = subprocess.run(
            ["lsappinfo", "front"],
            capture_output=True, text=True, timeout=2.0, check=False,
        )
        if result.returncode != 0:
            return None
        asn = result.stdout.strip()
        return asn or None
    except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
        logger.warning("lsappinfo front failed: %s", exc)
        return None


def _info_for_asn(asn: str) -> dict[str, str | None]:
    try:
        result = subprocess.run(
            ["lsappinfo", "info", asn],
            capture_output=True, text=True, timeout=2.0, check=False,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
        logger.warning("lsappinfo info failed: %s", exc)
        return {}
    if result.returncode != 0:
        return {}
    text = result.stdout
    bundle_id = _BUNDLE_ID_RE.search(text)
    version = _VERSION_RE.search(text)
    arch = _ARCH_RE.search(text)
    bundle_path = _BUNDLE_PATH_RE.search(text)
    checkin = _CHECKIN_RE.search(text)
    launched_at_iso: str | None = None
    if checkin:
        from datetime import datetime as _dt
        try:
            naive = _dt.strptime(checkin.group(1), "%Y/%m/%d %H:%M:%S")
            local = naive.astimezone()
            launched_at_iso = local.isoformat()
        except ValueError:
            launched_at_iso = None
    return {
        "bundle_id": bundle_id.group(1) if bundle_id else None,
        "version": version.group(1) if version else None,
        "arch": arch.group(1).lower() if arch else None,
        "bundle_path": bundle_path.group(1) if bundle_path else None,
        "launched_at": launched_at_iso,
    }


def _visible_apps_count() -> int:
    try:
        result = subprocess.run(
            ["lsappinfo", "visibleProcessList"],
            capture_output=True, text=True, timeout=2.0, check=False,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return 0
    if result.returncode != 0:
        return 0
    return sum(1 for tok in result.stdout.split() if tok.startswith("ASN:"))


_OSASCRIPT_BACKOFF_SECONDS = 60.0
_osascript_blocked_until: float = 0.0


def _frontmost_window_title() -> str | None:
    """Frontmost window title via `osascript` + System Events.

    Requires TCC Accessibility for the daemon's python binary. Returns None
    if Accessibility is denied, the app has no front window, or osascript
    times out / errors.
    """
    global _osascript_blocked_until
    now = time.monotonic()
    if now < _osascript_blocked_until:
        return None

    script = (
        'tell application "System Events" to '
        'name of front window of (first application process whose frontmost is true)'
    )
    try:
        result = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True, text=True, timeout=2.0, check=False,
        )
    except subprocess.TimeoutExpired:
        _osascript_blocked_until = now + _OSASCRIPT_BACKOFF_SECONDS
        logger.warning(
            "osascript timed out (likely Accessibility permission missing); "
            "backing off for %ds. Grant Accessibility to the venv python "
            "in System Settings → Privacy & Security → Accessibility.",
            int(_OSASCRIPT_BACKOFF_SECONDS),
        )
        return None
    except FileNotFoundError as exc:
        logger.warning("osascript not found: %s", exc)
        _osascript_blocked_until = now + _OSASCRIPT_BACKOFF_SECONDS
        return None
    if result.returncode != 0:
        logger.debug("osascript stderr: %s", result.stderr.strip())
        return None
    title = result.stdout.strip()
    return title or None


_AXDOCUMENT_SCRIPT = (
    'tell application "System Events"\n'
    '  try\n'
    '    set frontApp to (first application process whose frontmost is true)\n'
    '    set d to value of attribute "AXDocument" of front window of frontApp\n'
    '    if d is missing value then return ""\n'
    '    return d as string\n'
    '  on error\n'
    '    return ""\n'
    '  end try\n'
    'end tell'
)


def _split_file_path_and_name(raw: str) -> tuple[str, str]:
    """Parse the AXDocument string into (file_path, file_name).

    Handles file://..., http(s)://..., and plain POSIX paths.
    """
    raw = raw.strip()
    if not raw:
        return "", ""
    parsed = urlparse(raw)
    if parsed.scheme == "file":
        path = unquote(parsed.path)
        return path, os.path.basename(path) or path
    if parsed.scheme in ("http", "https", "ftp", "ftps"):
        segment = parsed.path.rstrip("/").rsplit("/", 1)[-1]
        name = unquote(segment) if segment else parsed.hostname or raw
        return raw, name
    return raw, os.path.basename(raw) or raw


def _frontmost_file() -> tuple[str, str]:
    """Return (file_path, file_name) for the focused app, or ("", "") if no
    document. Reuses ``_osascript_blocked_until`` so a single
    Accessibility-denied timeout suppresses BOTH window-title and AXDocument
    queries for the backoff window."""
    global _osascript_blocked_until
    now = time.monotonic()
    if now < _osascript_blocked_until:
        return "", ""

    try:
        result = subprocess.run(
            ["osascript", "-e", _AXDOCUMENT_SCRIPT],
            capture_output=True, text=True, timeout=2.0, check=False,
        )
    except subprocess.TimeoutExpired:
        _osascript_blocked_until = now + _OSASCRIPT_BACKOFF_SECONDS
        logger.warning(
            "osascript (AXDocument) timed out (likely Accessibility permission "
            "missing); backing off for %ds.",
            int(_OSASCRIPT_BACKOFF_SECONDS),
        )
        return "", ""
    except FileNotFoundError:
        return "", ""
    if result.returncode != 0:
        logger.debug("AXDocument osascript stderr: %s", result.stderr.strip())
        return "", ""
    return _split_file_path_and_name(result.stdout)


class FocusedAppTicker(AbstractTicker):
    name = "focused_app"

    def __init__(
        self,
        *,
        enabled: bool,
        poll_interval_seconds: int,
        knowledge_db_path=None,  # accepted for cli-builder symmetry; unused
        tmp_dir=None,
        host_slug: str,
        topic_prefix: str,
        availability_topic: str,
        publish_locked_state: bool = True,
    ) -> None:
        self._enabled = enabled
        self._poll_interval_seconds = poll_interval_seconds
        self._host_slug = host_slug
        self._topic_prefix = topic_prefix
        self._availability = availability_topic
        self._publish_locked_state = publish_locked_state
        # sentinels force a publish on the first run regardless of state
        self._last_published_bundle: str | None = "__unset__"
        self._last_published_window_title: str | None = "__unset__"
        self._last_published_file_path: str | None = "__unset__"

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def interval_seconds(self) -> int | None:
        return None  # self-paced

    def _state_topic(self, suffix: str) -> str:
        return screen_time_state_topic(
            self._topic_prefix, self._host_slug, f"focus/{suffix}"
        )

    def _visible_count_topic(self) -> str:
        return screen_time_state_topic(
            self._topic_prefix, self._host_slug, "visible_apps_count"
        )

    def _publish_change(
        self,
        mqtt: MqttPublisher,
        bundle_id: str | None,
        info: dict[str, str | None],
        window_title: str | None,
        file_path: str,
        file_name: str,
    ) -> None:
        previous = (
            self._last_published_bundle
            if self._last_published_bundle != "__unset__"
            else None
        )
        display = _resolve_friendly(bundle_id)
        previous_display = _resolve_friendly(previous)
        changed_at = datetime.now(tz=UTC).isoformat()

        mqtt.publish_state(self._state_topic("app"), display)
        mqtt.publish_state(self._state_topic("app_changed_at"), changed_at)
        mqtt.publish_state(self._state_topic("window_title"), window_title or "")
        mqtt.publish_state(self._state_topic("file_path"), file_path)
        mqtt.publish_state(self._state_topic("file_name"), file_name)
        mqtt.publish_attributes(
            self._state_topic("attrs"),
            {
                "bundle_id": bundle_id or "",
                "friendly_name": display,
                "previous": previous or "",
                "previous_friendly": previous_display,
                "changed_at": changed_at,
                "app_version": info.get("version") or "",
                "app_arch": info.get("arch") or "",
                "app_bundle_path": info.get("bundle_path") or "",
                "app_launched_at": info.get("launched_at") or "",
                "window_title": window_title or "",
                "file_path": file_path,
                "file_name": file_name,
            },
        )
        logger.info(
            "focused_app focus change: %s [%s] window=%r file=%r v=%s (was %s)",
            display, bundle_id, window_title, file_name, info.get("version"), previous,
        )
        self._last_published_bundle = bundle_id
        self._last_published_window_title = window_title
        self._last_published_file_path = file_path

    def _publish_volatile_only(
        self,
        mqtt: MqttPublisher,
        title: str | None,
        file_path: str,
        file_name: str,
    ) -> None:
        """Publish volatile fields when the focused app is unchanged but
        window title / file changed (e.g. browser tab switch)."""
        mqtt.publish_state(self._state_topic("window_title"), title or "")
        mqtt.publish_state(self._state_topic("file_path"), file_path)
        mqtt.publish_state(self._state_topic("file_name"), file_name)
        self._last_published_window_title = title
        self._last_published_file_path = file_path

    def _publish_visible_count(self, mqtt: MqttPublisher) -> None:
        count = _visible_apps_count()
        mqtt.publish_state(self._visible_count_topic(), count)

    async def run_once(self, mqtt: MqttPublisher) -> None:
        asn = _frontmost_asn()
        info = _info_for_asn(asn) if asn else {}
        target = info.get("bundle_id")
        window_title = _frontmost_window_title()
        file_path, file_name = _frontmost_file()

        if target is None and self._last_published_bundle == "__unset__":
            return

        if target != self._last_published_bundle:
            self._publish_change(mqtt, target, info, window_title, file_path, file_name)
        elif (
            window_title != self._last_published_window_title
            or file_path != self._last_published_file_path
        ):
            self._publish_volatile_only(mqtt, window_title, file_path, file_name)

        self._publish_visible_count(mqtt)

        await asyncio.sleep(self._poll_interval_seconds)

    async def publish_discovery(self, mqtt: MqttPublisher) -> None:
        device = mqtt.host_device_block()
        sensors = [
            ("app", "Focused App", None, "mdi:application", "attrs"),
            ("app_changed_at", "Focused App Changed At", "timestamp", "mdi:clock", None),
            ("window_title", "Focused Window Title", None, "mdi:window-restore", None),
            ("file_path", "Focused File Path", None, "mdi:file-outline", None),
            ("file_name", "Focused File Name", None, "mdi:file", None),
        ]
        for suffix, name, device_class, icon, attrs_suffix in sensors:
            uid = screen_time_unique_id(self._host_slug, "focused", suffix)
            mqtt.publish_discovery(
                component="sensor",
                unique_id=uid,
                payload=build_discovery_payload(
                    name=name,
                    unique_id=uid,
                    state_topic=self._state_topic(suffix),
                    availability_topic=self._availability,
                    device=device,
                    device_class=device_class,
                    icon=icon,
                    json_attributes_topic=(
                        self._state_topic(attrs_suffix) if attrs_suffix else None
                    ),
                ),
            )

        # Visible-apps-count sensor (separate from focus, but lives on the same device).
        vc_uid = screen_time_unique_id(self._host_slug, "visible_apps_count")
        mqtt.publish_discovery(
            component="sensor",
            unique_id=vc_uid,
            payload=build_discovery_payload(
                name="Visible Apps",
                unique_id=vc_uid,
                state_topic=self._visible_count_topic(),
                availability_topic=self._availability,
                device=device,
                state_class="measurement",
                unit_of_measurement="apps",
                icon="mdi:dots-grid",
            ),
        )
