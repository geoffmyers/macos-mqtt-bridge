"""Virtual meeting detection and tracking.

Determines whether the user is currently in a virtual meeting in any of:
  - Apple FaceTime (Mac app)
  - Zoom (Mac app)
  - Microsoft Teams (Mac app or web)
  - Google Meet (in Brave / Chrome / Safari / Firefox)

Detection works without requiring the meeting app to be in the foreground
— the signal is whether a candidate app is holding an active power
assertion that indicates media use (camera, microphone, or audio output).

Two evidence paths are checked, in order:

  1. **Direct assertion** — a known meeting app process itself holds a
     ``PreventUserIdleDisplaySleep`` (or related) assertion. This works for
     older Zoom builds, FaceTime via ``callservicesd``, and browsers with
     media-keyword-tagged assertions for Google Meet / Whereby / Jitsi.

  2. **CoreAudio-routed inference** — on macOS Sequoia/Tahoe (14+), Teams
     2.0 (``com.microsoft.teams2``) and current Zoom builds delegate audio
     session management to ``coreaudiod``, which holds the
     ``PreventUserIdleDisplaySleep`` assertion on the app's behalf. The
     Teams/Zoom process itself never appears in ``pmset -g assertions``
     during a meeting. So when ``coreaudiod`` is asserting ≥ 2 active
     audio contexts (typical of meetings: input + output streams; atypical
     of single-stream music playback) and a known native meeting app
     process is currently running (``pgrep -x``), we infer a meeting and
     attribute it to that app.

Specifically:
  - Native apps (Zoom, Teams, FaceTime): qualify via path 1 if the app
    holds its own assertion, else via path 2 if ``coreaudiod`` holds the
    assertion and the app is running.
  - FaceTime: also qualifies via ``callservicesd`` holding a call-services
    assertion (FaceTime delegates the actual call state to that daemon).
  - Browsers: qualify only via path 1, requiring the assertion name to
    contain a meeting keyword (audio/video/webrtc/media/etc.). Browsers
    take ``PreventUserIdleDisplaySleep`` for any video playback (YouTube,
    etc.), so the keyword filter avoids false positives. Path 2 is not
    used for browsers because they're typically always running.

Publishes six HA-discoverable sensors:

  binary_sensor.<host>_virtual_meeting_in_progress
  sensor.<host>_virtual_meeting_last_application
  sensor.<host>_virtual_meeting_last_started_at
  sensor.<host>_virtual_meeting_last_ended_at
  sensor.<host>_virtual_meeting_last_duration
  sensor.<host>_virtual_meeting_last_title

State transitions are sticky: ``last_*`` sensors keep their values across
meetings (they describe the most-recently-ended meeting), and the
in_progress sensor flips OFF only after ``end_grace_seconds`` of
consecutive "not detected" ticks (smooths over momentary pmset gaps).
"""

from __future__ import annotations

import logging
import re
import subprocess
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from macos_bridge.mqtt import MqttPublisher, build_discovery_payload
from macos_bridge.phases.base import (
    AbstractTicker,
    screen_time_state_topic,
    screen_time_unique_id,
)

logger = logging.getLogger(__name__)

# Process-name substring → (friendly app label, kind). Substrings are
# lowercased before matching against ``pid X(processname)`` strings.
# Order matters: more-specific patterns are listed first so they win.
_CANDIDATE_APPS: list[tuple[str, str, str]] = [
    ("zoom.us", "Zoom", "native"),
    ("microsoft teams", "Microsoft Teams", "native"),
    ("msteams", "Microsoft Teams", "native"),
    ("facetime", "Apple FaceTime", "native"),
    ("callservicesd", "Apple FaceTime", "system"),
    ("brave browser", "Google Meet", "browser"),
    ("google chrome helper", "Google Meet", "browser"),
    ("google chrome", "Google Meet", "browser"),
    ("safari", "Google Meet", "browser"),
    ("firefox", "Google Meet", "browser"),
    ("brave", "Google Meet", "browser"),  # generic fallback after "brave browser"
]

# pmset assertion-name substrings that mark a media/meeting context. Used
# to filter the inherently noisier browser signals; native apps qualify
# without requiring a keyword match.
_MEETING_KEYWORDS = (
    "meeting", "call", "in progress",
    "audio is being used", "audio active", "audio in use",
    "video is being used", "webrtc", "media playing",
    "facetime", "ftcall", "call services",
    "screen sharing", "screen share",
)

# Assertion types that indicate the app is keeping the user-presented
# state alive — meetings, video playback, fullscreen content, etc.
# The Prevent* names are the modern public constants; NoIdleSleepAssertion
# is the older synonym still emitted by Chromium-family browsers (Brave,
# Chrome) for WebRTC sessions.
_MEDIA_ASSERTION_TYPES = {
    "PreventUserIdleDisplaySleep",
    "PreventUserIdleSystemSleep",
    "PreventSystemSleep",
    "NoIdleSleepAssertion",
}

# Each pmset line listing an assertion looks like:
#   pid 1234(Process Name): [0xABCDEF0123456789] 00:01:36 PreventX named: "label text"
_PMSET_LINE_RE = re.compile(
    r"pid\s+(\d+)\((.*?)\):\s+\[[^\]]+\]\s+\S+\s+(\S+)\s+named:\s+\"([^\"]*)\""
)

# Process names (matched exactly via ``pgrep -x``) that, when running,
# attribute a coreaudiod-routed audio session to a virtual meeting. These
# are the apps' actual main-executable names as macOS reports them (no
# path, no extension). Order matters: the first hit wins.
_NATIVE_MEETING_PROCESSES: list[tuple[str, str]] = [
    # (pgrep -x pattern, friendly app label)
    ("MSTeams", "Microsoft Teams"),                  # Teams 2.0
    ("Microsoft Teams", "Microsoft Teams"),          # Teams classic
    ("Microsoft Teams (work or school)", "Microsoft Teams"),
    ("Microsoft Teams classic", "Microsoft Teams"),
    ("zoom.us", "Zoom"),
    ("FaceTime", "Apple FaceTime"),
]

# Minimum number of ``coreaudiod`` audio-context assertions that count as
# an "active meeting-like audio session." Single-stream music playback
# typically produces 1 context; meetings (mic input + speaker output, and
# often system sounds and screen-share audio) consistently produce ≥ 2.
_COREAUDIOD_MIN_CONTEXTS = 2


def _run_pmset_assertions(timeout: float = 3.0) -> str:
    try:
        result = subprocess.run(
            ["pmset", "-g", "assertions"],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
        logger.debug("pmset failed: %s", exc)
        return ""
    return result.stdout if result.returncode == 0 else ""


def _classify_process(process_name: str) -> tuple[str, str] | None:
    """Return (friendly_app, kind) for known meeting-app process names, else
    None. Match is case-insensitive substring."""
    lowered = process_name.lower()
    for needle, friendly, kind in _CANDIDATE_APPS:
        if needle in lowered:
            return friendly, kind
    return None


def _assertion_indicates_meeting(
    kind: str, assertion_type: str, assertion_name: str
) -> bool:
    """Decide whether a single pmset assertion line counts as evidence of an
    active meeting for an app of the given ``kind``."""
    if assertion_type not in _MEDIA_ASSERTION_TYPES:
        return False
    if kind == "browser":
        # Browsers take display-sleep assertions for any video/audio playback;
        # require the assertion name to mention meeting/media specifically.
        name_l = assertion_name.lower()
        return any(k in name_l for k in _MEETING_KEYWORDS)
    # Native (Zoom/Teams/FaceTime) and system (callservicesd): the bare
    # presence of a media-class assertion is a strong meeting signal.
    return True


def _count_coreaudiod_audio_contexts(pmset_text: str) -> int:
    """Count the active ``coreaudiod`` audio-context assertions in pmset
    output. Each audio I/O stream coreaudiod is keeping alive shows up as
    a separate ``com.apple.audio.<device-or-context>.context.preventuser*``
    assertion. macOS uses several name shapes:

      com.apple.audio.context<N>.preventuseridledisplaysleep
      com.apple.audio.BuiltInMicrophoneDevice.context.preventuseridlesleep
      com.apple.audio.BuiltInSpeakerDevice.context.preventuseridlesleep
      com.apple.audio.AVVCAggregateDevice-<id>.context.preventuseridlesleep

    Music playback typically produces 1 stream context; meetings (mic +
    speaker + often an aggregate VCAudio device) consistently produce ≥ 2.
    """
    count = 0
    for line in pmset_text.splitlines():
        m = _PMSET_LINE_RE.search(line)
        if m is None:
            continue
        _pid, process_name, assertion_type, assertion_name = m.groups()
        if process_name != "coreaudiod":
            continue
        if assertion_type not in _MEDIA_ASSERTION_TYPES:
            continue
        if assertion_name.startswith("com.apple.audio.") and ".context" in assertion_name:
            count += 1
    return count


def _detect_running_native_meeting_app(
    timeout: float = 2.0,
) -> tuple[str, str, str] | None:
    """Return (friendly, "native_inferred", process_name) for the first
    process from ``_NATIVE_MEETING_PROCESSES`` currently running, else None.

    Uses ``pgrep -x`` (exact basename match) so helper processes like
    ``Microsoft Teams Helper`` don't false-positive when the main app is
    closed.
    """
    for pattern, friendly in _NATIVE_MEETING_PROCESSES:
        try:
            result = subprocess.run(
                ["pgrep", "-x", pattern],
                capture_output=True, text=True, timeout=timeout, check=False,
            )
        except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
            logger.debug("pgrep -x %r failed: %s", pattern, exc)
            continue
        if result.returncode == 0 and result.stdout.strip():
            return friendly, "native_inferred", pattern
    return None


def _detect_active_app() -> tuple[str, str, str] | None:
    """Inspect ``pmset -g assertions`` (and ``pgrep`` as a fallback) and
    return the (friendly_app, kind, process_name) of the meeting app
    currently active, or None if nothing matches.

    Two paths:
      1. Direct match — a known candidate process holds a media-class
         assertion itself.
      2. CoreAudio-routed inference — ``coreaudiod`` holds ≥
         ``_COREAUDIOD_MIN_CONTEXTS`` audio-context assertions AND a known
         native meeting app process is running (modern Teams/Zoom/etc.).

    When multiple candidates qualify simultaneously (rare), prefer
    native > system > native_inferred > browser.
    """
    text = _run_pmset_assertions()
    if not text:
        return None

    matches: list[tuple[str, str, str]] = []  # (friendly, kind, process_name)
    for line in text.splitlines():
        m = _PMSET_LINE_RE.search(line)
        if m is None:
            continue
        _pid, process_name, assertion_type, assertion_name = m.groups()
        cls = _classify_process(process_name)
        if cls is None:
            continue
        friendly, kind = cls
        if not _assertion_indicates_meeting(kind, assertion_type, assertion_name):
            continue
        matches.append((friendly, kind, process_name))

    # Path 2: coreaudiod-routed inference. Only consult this if the direct
    # path found nothing native/system — a direct match is always more
    # precise. (A direct browser match is left as-is; coreaudiod inference
    # for a browser-only scenario is more ambiguous and intentionally not
    # added.)
    direct_native_or_system = any(k in ("native", "system") for _, k, _ in matches)
    if (
        not direct_native_or_system
        and _count_coreaudiod_audio_contexts(text) >= _COREAUDIOD_MIN_CONTEXTS
    ):
        inferred = _detect_running_native_meeting_app()
        if inferred is not None:
            matches.append(inferred)

    if not matches:
        return None

    # Preference order: native (direct) > system (callservicesd) >
    # native_inferred (via coreaudiod + pgrep) > browser.
    priority = {"native": 0, "system": 1, "native_inferred": 2, "browser": 3}
    matches.sort(key=lambda m: priority.get(m[1], 99))
    return matches[0]


def _frontmost_app_window_title(process_name: str) -> str | None:
    """Best-effort: get the front window title for ``process_name`` via
    ``lsappinfo``. Works without focus for some apps (Zoom typically
    publishes its meeting title even when backgrounded), fails silently
    for others."""
    try:
        result = subprocess.run(
            ["lsappinfo", "find", f"ApplicationType=Foreground"],
            capture_output=True, text=True, timeout=2.0, check=False,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None
    # lsappinfo find doesn't directly take a process name; the output is
    # ASN tokens. Try the simpler `info -app <bundle-or-name>` form.
    try:
        result = subprocess.run(
            ["lsappinfo", "info", "-app", process_name],
            capture_output=True, text=True, timeout=2.0, check=False,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None
    if result.returncode != 0:
        return None
    # Window titles aren't reliably in lsappinfo info output. Return None
    # so the caller falls back to a generic label. Keeping this seam open
    # for a future implementation that uses the AXUIElement APIs.
    return None


class VirtualMeetingsTicker(AbstractTicker):
    name = "virtual_meetings"

    def __init__(
        self,
        *,
        enabled: bool,
        interval_seconds: int,
        host_slug: str,
        topic_prefix: str,
        availability_topic: str,
        end_grace_ticks: int = 2,
        on_meeting_started: Callable[[str], None] | None = None,
        on_meeting_ended: Callable[[str], None] | None = None,
    ) -> None:
        self._enabled = enabled
        self._interval_seconds = interval_seconds
        self._host_slug = host_slug
        self._topic_prefix = topic_prefix
        self._availability = availability_topic
        # When detection drops, wait this many consecutive negative ticks
        # before flipping in_progress OFF — protects against momentary
        # pmset gaps mid-meeting.
        self._end_grace_ticks = max(1, int(end_grace_ticks))
        self._on_meeting_started = on_meeting_started
        self._on_meeting_ended = on_meeting_ended

        # Live state machine.
        self._in_progress: bool = False
        self._current_app: str | None = None
        self._current_process: str | None = None
        self._current_title: str | None = None
        self._started_at: datetime | None = None
        self._negative_streak: int = 0

        # Historical "last meeting" record — survives the in_progress flip.
        self._last_app: str | None = None
        self._last_title: str | None = None
        self._last_started_at: datetime | None = None
        self._last_ended_at: datetime | None = None
        self._last_duration_seconds: int | None = None

        # Sentinel forces a publish on the first tick regardless of state.
        self._published_in_progress: bool | None = None

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def interval_seconds(self) -> int | None:
        return self._interval_seconds

    def _state_topic(self, suffix: str) -> str:
        return screen_time_state_topic(
            self._topic_prefix, self._host_slug, f"virtual_meeting/{suffix}"
        )

    def _unique_id(self, suffix: str) -> str:
        return screen_time_unique_id(self._host_slug, "virtual_meeting", suffix)

    def _publish_current(self, mqtt: MqttPublisher) -> None:
        new_state = "ON" if self._in_progress else "OFF"
        if new_state != self._published_in_progress:
            mqtt.publish_state(self._state_topic("in_progress"), new_state)
            self._published_in_progress = new_state

    def _publish_last(self, mqtt: MqttPublisher) -> None:
        if self._last_app is not None:
            mqtt.publish_state(self._state_topic("last_application"), self._last_app)
        if self._last_title is not None:
            mqtt.publish_state(self._state_topic("last_title"), self._last_title)
        if self._last_started_at is not None:
            mqtt.publish_state(
                self._state_topic("last_started_at"),
                self._last_started_at.isoformat(),
            )
        if self._last_ended_at is not None:
            mqtt.publish_state(
                self._state_topic("last_ended_at"),
                self._last_ended_at.isoformat(),
            )
        if self._last_duration_seconds is not None:
            mqtt.publish_state(
                self._state_topic("last_duration"), self._last_duration_seconds
            )

    async def run_once(self, mqtt: MqttPublisher) -> None:
        detection = _detect_active_app()

        if detection is not None:
            friendly, kind, process_name = detection
            self._negative_streak = 0
            if not self._in_progress:
                # Transition: meeting just started.
                self._in_progress = True
                self._current_app = friendly
                self._current_process = process_name
                self._started_at = datetime.now(tz=UTC)
                self._current_title = (
                    _frontmost_app_window_title(process_name) or friendly
                )
                # Update "last_*" historical snapshot at start so an in-progress
                # meeting's app+title appear in HA right away (rather than only
                # after it ends).
                self._last_app = self._current_app
                self._last_title = self._current_title
                self._last_started_at = self._started_at
                self._last_ended_at = None
                self._last_duration_seconds = None
                logger.info(
                    "virtual_meetings: meeting started — %s (process=%s, kind=%s)",
                    friendly, process_name, kind,
                )
                if self._on_meeting_started is not None:
                    try:
                        self._on_meeting_started(friendly)
                    except Exception:
                        logger.exception(
                            "virtual_meetings: on_meeting_started callback failed"
                        )
            elif friendly != self._current_app:
                # Active meeting switched apps (rare — e.g. user joined a
                # second meeting in another app while the first was still
                # holding its assertion).
                old_app = self._current_app
                logger.info(
                    "virtual_meetings: active app changed %s → %s",
                    old_app, friendly,
                )
                self._current_app = friendly
                self._current_process = process_name
                self._last_app = friendly
                if self._on_meeting_ended is not None and old_app is not None:
                    try:
                        self._on_meeting_ended(old_app)
                    except Exception:
                        logger.exception(
                            "virtual_meetings: on_meeting_ended callback failed"
                        )
                if self._on_meeting_started is not None:
                    try:
                        self._on_meeting_started(friendly)
                    except Exception:
                        logger.exception(
                            "virtual_meetings: on_meeting_started callback failed"
                        )
        else:
            # No candidate app currently holds a meeting assertion.
            if self._in_progress:
                self._negative_streak += 1
                if self._negative_streak >= self._end_grace_ticks:
                    # Transition: meeting just ended.
                    ended = datetime.now(tz=UTC)
                    started = self._started_at or ended
                    duration = int((ended - started).total_seconds())
                    ended_app = self._current_app
                    self._in_progress = False
                    self._last_ended_at = ended
                    self._last_duration_seconds = max(0, duration)
                    logger.info(
                        "virtual_meetings: meeting ended — %s, duration=%ds",
                        ended_app, self._last_duration_seconds,
                    )
                    self._current_app = None
                    self._current_process = None
                    self._current_title = None
                    self._started_at = None
                    self._negative_streak = 0
                    if self._on_meeting_ended is not None and ended_app is not None:
                        try:
                            self._on_meeting_ended(ended_app)
                        except Exception:
                            logger.exception(
                                "virtual_meetings: on_meeting_ended callback failed"
                            )

        self._publish_current(mqtt)
        self._publish_last(mqtt)

    async def publish_discovery(self, mqtt: MqttPublisher) -> None:
        device = mqtt.host_device_block()

        # Binary sensor: in progress
        in_progress_payload: dict[str, Any] = build_discovery_payload(
            name="Virtual Meetings - In Progress",
            unique_id=self._unique_id("in_progress"),
            state_topic=self._state_topic("in_progress"),
            availability_topic=self._availability,
            device=device,
            icon="mdi:video-account",
        )
        in_progress_payload["payload_on"] = "ON"
        in_progress_payload["payload_off"] = "OFF"
        mqtt.publish_discovery(
            component="binary_sensor",
            unique_id=self._unique_id("in_progress"),
            payload=in_progress_payload,
        )

        # String + numeric sensors: last meeting
        sensors: list[
            tuple[str, str, str, str | None, str | None, str | None]
        ] = [
            # (suffix, name, icon, device_class, unit, state_class)
            ("last_application", "Virtual Meetings - Last Application", "mdi:application",
             None, None, None),
            ("last_started_at", "Virtual Meetings - Last Started At", "mdi:clock-start",
             "timestamp", None, None),
            ("last_ended_at", "Virtual Meetings - Last Ended At", "mdi:clock-end",
             "timestamp", None, None),
            ("last_duration", "Virtual Meetings - Last Duration", "mdi:timer-outline",
             "duration", "s", "measurement"),
            ("last_title", "Virtual Meetings - Last Title", "mdi:format-title",
             None, None, None),
        ]
        for suffix, name, icon, device_class, unit, state_class in sensors:
            # last_* sensors are HISTORICAL — they describe the most
            # recently ended meeting, so they should keep their value
            # visible even when the bridge is offline.
            mqtt.publish_discovery(
                component="sensor",
                unique_id=self._unique_id(suffix),
                payload=build_discovery_payload(
                    name=name,
                    unique_id=self._unique_id(suffix),
                    state_topic=self._state_topic(suffix),
                    availability_topic=None,
                    device=device,
                    device_class=device_class,
                    unit_of_measurement=unit,
                    state_class=state_class,
                    icon=icon,
                ),
            )
