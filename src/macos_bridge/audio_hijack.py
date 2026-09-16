"""Audio Hijack session control via AppleScript.

Starts and stops named Audio Hijack recording sessions in response to
virtual meeting and phone call events detected by the bridge.

Audio Hijack must already be running for AppleScript to succeed — the
bridge does not launch it. Sessions are matched by exact name.

Trigger mapping (all configured via config.yaml audio_hijack section):

  Zoom meeting detected        → zoom_session
  Microsoft Teams detected     → teams_session
  Apple FaceTime detected      → facetime_session
  Brave / browser Meet detected → browser_session
  Phone ringing / outgoing     → phone_session
"""

from __future__ import annotations

import logging
import subprocess

log = logging.getLogger(__name__)


class AudioHijackController:
    """Controls Audio Hijack sessions via AppleScript.

    Call on_meeting_started / on_meeting_ended with the VirtualMeetingsTicker
    friendly-app label ("Zoom", "Microsoft Teams", "Apple FaceTime",
    "Google Meet") to start/stop the matching session. Call on_phone_event
    with the realtime call event path to handle phone recordings.

    With auto_stop=True (default), the bridge only stops sessions it started
    — manual recordings from the Audio Hijack UI are left untouched.
    """

    def __init__(
        self,
        *,
        zoom_session: str | None = None,
        teams_session: str | None = None,
        facetime_session: str | None = None,
        browser_session: str | None = None,
        phone_session: str | None = None,
        auto_stop: bool = True,
    ) -> None:
        self._sessions: dict[str, str | None] = {
            "Zoom": zoom_session,
            "Microsoft Teams": teams_session,
            "Apple FaceTime": facetime_session,
            "Google Meet": browser_session,
        }
        self._phone_session = phone_session
        self._auto_stop = auto_stop
        self._started: set[str] = set()

    def _applescript(self, script: str) -> bool:
        try:
            result = subprocess.run(
                ["osascript", "-e", script],
                capture_output=True,
                text=True,
                timeout=5.0,
                check=False,
            )
        except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
            log.warning("audio_hijack: osascript error: %s", exc)
            return False
        if result.returncode != 0:
            log.warning(
                "audio_hijack: osascript exited %d: %s",
                result.returncode,
                result.stderr.strip(),
            )
            return False
        return True

    def start_session(self, name: str) -> None:
        script = (
            'tell application "Audio Hijack" to '
            f'start (first session whose name is "{name}")'
        )
        if self._applescript(script):
            self._started.add(name)
            log.info("audio_hijack: started session %r", name)

    def stop_session(self, name: str) -> None:
        if self._auto_stop and name not in self._started:
            log.debug("audio_hijack: skip stop of %r (not started by bridge)", name)
            return
        script = (
            'tell application "Audio Hijack" to '
            f'stop (first session whose name is "{name}")'
        )
        if self._applescript(script):
            self._started.discard(name)
            log.info("audio_hijack: stopped session %r", name)

    def on_meeting_started(self, friendly_app: str) -> None:
        session = self._sessions.get(friendly_app)
        if session:
            self.start_session(session)

    def on_meeting_ended(self, friendly_app: str) -> None:
        session = self._sessions.get(friendly_app)
        if session:
            self.stop_session(session)

    def on_phone_event(self, event_path: str) -> None:
        if not self._phone_session:
            return
        if event_path in ("phone/ringing", "phone/outgoing_started"):
            self.start_session(self._phone_session)
        elif event_path == "phone/realtime_ended":
            self.stop_session(self._phone_session)
