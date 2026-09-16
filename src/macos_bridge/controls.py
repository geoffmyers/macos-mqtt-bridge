"""Inbound HA controls — buttons, switches, numbers, selects, text inputs
that turn into shell calls on this Mac.

Each control class self-describes:

  - ``component``         — HA MQTT integration name (button/switch/number/...)
  - ``slug``              — unique-id suffix; topic suffix
  - ``name``              — HA-visible entity name (with "Controls - " prefix)
  - ``discovery_payload`` — config block published once at startup
  - ``handle``            — runs the macOS shell command on each MQTT message

The shared MQTT subscription happens in ``ControlsHandler``; runtime.py
calls ``ControlsHandler.start()`` after the publisher connects.

Topic layout (parallel with the read-only sensors):

  command:    macos/<host>/controls/<slug>/{set,press,send}
  state:      macos/<host>/controls/<slug>/state   (when applicable)
  discovery:  homeassistant/<component>/macos_<host>_<slug>/config

Volume + mute controls reuse the existing ``system/volume_level`` and
``system/volume_muted`` state topics (already maintained by the
system_state ticker, default 30s cadence) so HA gets state confirmation
without the controls module having to publish back. Other controls
that have no natural read-side counterpart publish their own state
topic on each successful command.

DESTRUCTIVE actions (sleep / restart / shut down) default to disabled
in ControlsConfig so a compromised broker can't trivially nuke the
machine. The user opts in per-control in config.yaml.
"""

from __future__ import annotations

import logging
import shlex
import shutil
import subprocess
from dataclasses import dataclass, field
from typing import Any, Callable

from macos_bridge.config import Config, ControlsConfig
from macos_bridge.mqtt import MqttPublisher

log = logging.getLogger(__name__)


# ---- shell command helpers --------------------------------------------------

def _run(args: list[str], *, timeout: float = 5.0) -> tuple[int, str, str]:
    """Run a command; return (returncode, stdout, stderr). Never raises."""
    try:
        proc = subprocess.run(
            args, capture_output=True, text=True, timeout=timeout, check=False
        )
        return proc.returncode, proc.stdout, proc.stderr
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as exc:
        log.warning("controls: %s failed: %s", args[0], exc)
        return 1, "", str(exc)


def _osascript(script: str, *, timeout: float = 5.0) -> tuple[int, str, str]:
    """Run AppleScript via osascript -e. Single-script invocation."""
    return _run(["osascript", "-e", script], timeout=timeout)


# ---- caffeinate subprocess management ---------------------------------------

class _CaffeinateManager:
    """Manages a single ``caffeinate -di`` background process. While it's
    alive, macOS won't sleep the display or system. Stopping the manager
    kills the subprocess, restoring normal sleep behavior."""

    def __init__(self) -> None:
        self._proc: subprocess.Popen | None = None

    @property
    def is_active(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def enable(self) -> None:
        if self.is_active:
            return
        try:
            # -d: prevent display sleep; -i: prevent system idle sleep
            self._proc = subprocess.Popen(
                ["caffeinate", "-di"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            log.info("controls: caffeinate enabled (pid=%d)", self._proc.pid)
        except (FileNotFoundError, OSError) as exc:
            log.warning("controls: caffeinate spawn failed: %s", exc)
            self._proc = None

    def disable(self) -> None:
        if self._proc is None:
            return
        try:
            self._proc.terminate()
            self._proc.wait(timeout=2.0)
        except subprocess.TimeoutExpired:
            self._proc.kill()
        except OSError as exc:
            log.warning("controls: caffeinate terminate failed: %s", exc)
        finally:
            log.info("controls: caffeinate disabled")
            self._proc = None


# ---- control descriptor + concrete handlers --------------------------------

@dataclass
class Control:
    """One inbound HA control. ``component`` is the HA MQTT integration
    name; ``slug`` is the topic + unique-id suffix; ``handler`` runs the
    side effect on each command. ``discovery_extra`` is merged into the
    base discovery payload so component-specific fields (min/max for
    numbers, options for selects, payload_on/off for switches, etc.)
    can be set per control."""

    component: str
    slug: str
    name: str
    icon: str
    handler: Callable[[bytes, "ControlsHandler"], dict | None]
    # When True, this control owns its own state_topic (controls/<slug>/state).
    # When False, the discovery payload either has no state_topic (button,
    # text) or reuses an existing topic explicitly via discovery_extra.
    own_state_topic: bool = False
    discovery_extra: dict[str, Any] = field(default_factory=dict)


# ---- handler implementations -----------------------------------------------

def _handle_volume_set(payload: bytes, _ctx: "ControlsHandler") -> dict | None:
    try:
        target = int(float(payload.decode().strip()))
    except (ValueError, UnicodeDecodeError):
        log.warning("controls: bad volume payload: %r", payload)
        return None
    target = max(0, min(100, target))
    rc, _, err = _osascript(f"set volume output volume {target}")
    if rc != 0:
        log.warning("controls: set volume failed: %s", err.strip())
    log.info("controls: volume set to %d%%", target)
    return None  # state will sync via system_state ticker


def _handle_mute_set(payload: bytes, _ctx: "ControlsHandler") -> dict | None:
    value = payload.decode().strip().upper()
    muted = value == "ON"
    rc, _, err = _osascript(f"set volume output muted {'true' if muted else 'false'}")
    if rc != 0:
        log.warning("controls: mute toggle failed: %s", err.strip())
    log.info("controls: mute -> %s", "ON" if muted else "OFF")
    return None  # state will sync via system_state ticker


def _handle_lock_screen(_payload: bytes, _ctx: "ControlsHandler") -> dict | None:
    # Sleeps the display, which immediately triggers the screen lock
    # (assuming the user has "Require password after sleep/screen
    # saver begins" set, which is the default since macOS Catalina).
    rc, _, err = _run(["pmset", "displaysleepnow"])
    if rc != 0:
        log.warning("controls: lock screen failed: %s", err.strip())
    log.info("controls: lock screen pressed")
    return None


def _handle_screensaver_set(payload: bytes, _ctx: "ControlsHandler") -> dict | None:
    """ON  -> start ScreenSaverEngine via ``open -a``.
    OFF -> ``killall ScreenSaverEngine``.
    State is read separately from the system_state ticker's
    ``system/screensaver_running`` topic, so HA syncs even when the
    screensaver was started/stopped externally (idle timer, hot
    corner, mouse jiggle)."""
    value = payload.decode().strip().upper()
    if value == "ON":
        rc, _, err = _run(["open", "-a", "ScreenSaverEngine"])
        if rc != 0:
            log.warning("controls: screensaver start failed: %s", err.strip())
        log.info("controls: screensaver started")
    else:
        rc, _, err = _run(["killall", "ScreenSaverEngine"])
        # killall returns 1 when no matching process exists, which is
        # a perfectly valid "already off" outcome — don't warn on that.
        if rc != 0 and rc != 1:
            log.warning("controls: screensaver stop failed: %s", err.strip())
        log.info("controls: screensaver stopped")
    return None


def _handle_sleep(_payload: bytes, _ctx: "ControlsHandler") -> dict | None:
    log.warning("controls: SLEEP command received — invoking System Events")
    rc, _, err = _osascript('tell application "System Events" to sleep')
    if rc != 0:
        log.warning("controls: sleep failed: %s", err.strip())
    return None


def _handle_restart(_payload: bytes, _ctx: "ControlsHandler") -> dict | None:
    log.warning("controls: RESTART command received — invoking System Events")
    rc, _, err = _osascript('tell application "System Events" to restart')
    if rc != 0:
        log.warning("controls: restart failed: %s", err.strip())
    return None


def _handle_shutdown(_payload: bytes, _ctx: "ControlsHandler") -> dict | None:
    log.warning("controls: SHUTDOWN command received — invoking System Events")
    rc, _, err = _osascript('tell application "System Events" to shut down')
    if rc != 0:
        log.warning("controls: shutdown failed: %s", err.strip())
    return None


def _osascript_string_literal(value: str) -> str:
    """Wrap a Python string as an AppleScript string literal — escapes
    backslashes and double quotes, then wraps in double quotes. Avoids
    shell injection via osascript -e."""
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _handle_display_notification(payload: bytes, _ctx: "ControlsHandler") -> dict | None:
    try:
        text = payload.decode("utf-8", errors="replace").strip()
    except Exception:  # noqa: BLE001
        return None
    if not text:
        return None
    # AppleScript: display notification "body" with title "title"
    body = _osascript_string_literal(text)
    title = _osascript_string_literal("Home Assistant")
    rc, _, err = _osascript(f"display notification {body} with title {title}")
    if rc != 0:
        log.warning("controls: display notification failed: %s", err.strip())
    log.info("controls: notification displayed (%d chars)", len(text))
    return None


def _handle_speak(payload: bytes, _ctx: "ControlsHandler") -> dict | None:
    text = payload.decode("utf-8", errors="replace").strip()
    if not text:
        return None
    # ``say`` accepts text as a positional arg — no shell escaping needed
    # because we're passing it through subprocess argv, not a shell string.
    rc, _, err = _run(["say", text], timeout=60.0)
    if rc != 0:
        log.warning("controls: say failed: %s", err.strip())
    log.info("controls: spoke %d chars", len(text))
    return None


def _handle_open_url(payload: bytes, _ctx: "ControlsHandler") -> dict | None:
    url = payload.decode("utf-8", errors="replace").strip()
    if not url:
        return None
    # Reject obvious shell garbage; ``open`` itself is safe with argv but
    # it will happily open file:// URLs that read sensitive paths if the
    # user wires up bad automations. Restrict to http(s)://, mailto:,
    # and a small set of known-safe schemes.
    safe_schemes = ("http://", "https://", "mailto:", "tel:", "facetime://", "imessage://")
    if not any(url.startswith(s) for s in safe_schemes):
        log.warning("controls: rejecting open_url with unsafe scheme: %r", url[:64])
        return None
    rc, _, err = _run(["open", url])
    if rc != 0:
        log.warning("controls: open url failed: %s", err.strip())
    log.info("controls: opened %s", url[:80])
    return None


def _media_via_nowplaying_cli(action: str) -> bool:
    """Try ``nowplaying-cli <action>`` first; return True on success.
    Falls back to AppleScript media keys when nowplaying-cli isn't
    installed."""
    if shutil.which("nowplaying-cli") is None:
        return False
    rc, _, _ = _run(["nowplaying-cli", action])
    return rc == 0


def _media_via_applescript(action: str) -> None:
    """AppleScript fallback that talks to Music.app directly. Doesn't
    work for arbitrary media apps — Spotify / browser players have their
    own scripting dictionaries — but covers the common case."""
    cmd_map = {
        "play": 'tell application "Music" to play',
        "pause": 'tell application "Music" to pause',
        "next": 'tell application "Music" to next track',
        "previous": 'tell application "Music" to previous track',
    }
    cmd = cmd_map.get(action)
    if cmd:
        _osascript(cmd)


def _handle_media_play_set(payload: bytes, _ctx: "ControlsHandler") -> dict | None:
    """ON  -> resume playback (``nowplaying-cli play``, fallback Music.app).
    OFF -> pause (``nowplaying-cli pause``).
    State is read from the now_playing ticker's ``now_playing/state``
    topic via value_template, so HA reflects external play/pause
    activity (e.g. clicking pause in Spotify directly) within the
    next now_playing tick (5s default)."""
    value = payload.decode().strip().upper()
    if value == "ON":
        if not _media_via_nowplaying_cli("play"):
            _media_via_applescript("play")
        log.info("controls: media -> play")
    else:
        if not _media_via_nowplaying_cli("pause"):
            _media_via_applescript("pause")
        log.info("controls: media -> pause")
    return None


def _handle_media_next(_payload: bytes, _ctx: "ControlsHandler") -> dict | None:
    if not _media_via_nowplaying_cli("next"):
        _media_via_applescript("next")
    log.info("controls: media next")
    return None


def _handle_media_prev(_payload: bytes, _ctx: "ControlsHandler") -> dict | None:
    if not _media_via_nowplaying_cli("previous"):
        _media_via_applescript("previous")
    log.info("controls: media prev")
    return None


def _handle_caffeinate_set(payload: bytes, ctx: "ControlsHandler") -> dict | None:
    value = payload.decode().strip().upper()
    if value == "ON":
        ctx.caffeinate.enable()
    else:
        ctx.caffeinate.disable()
    return {"state": "ON" if ctx.caffeinate.is_active else "OFF"}


# ---- handler registry / per-config builder ---------------------------------

def _build_controls(cfg: ControlsConfig) -> list[Control]:
    """Materialize the list of enabled controls. Skips any whose
    per-control flag in ControlsConfig is False — including the
    destructive ones, which default to False."""
    controls: list[Control] = []

    if cfg.volume:
        controls.append(Control(
            component="number",
            slug="volume",
            name="Controls - Volume",
            icon="mdi:volume-high",
            handler=_handle_volume_set,
            own_state_topic=False,
            discovery_extra={
                # Reuse the system_state volume_level topic as the read
                # side; HA renders the slider's position from this.
                # ``state_topic`` is filled in by ControlsHandler since
                # it depends on the live host_slug + topic_prefix.
                "_reuse_state_topic": "system/volume_level",
                "min": 0, "max": 100, "step": 1,
                "unit_of_measurement": "%",
                "mode": "slider",
                "command_template": "{{ value | int }}",
            },
        ))

    if cfg.mute:
        controls.append(Control(
            component="switch",
            slug="mute",
            name="Controls - Mute",
            icon="mdi:volume-off",
            handler=_handle_mute_set,
            own_state_topic=False,
            discovery_extra={
                "_reuse_state_topic": "system/volume_muted",
                "payload_on": "ON",
                "payload_off": "OFF",
                "state_on": "ON",
                "state_off": "OFF",
            },
        ))

    if cfg.lock_screen:
        controls.append(Control(
            component="button",
            slug="lock_screen",
            name="Controls - Lock Screen",
            icon="mdi:lock",
            handler=_handle_lock_screen,
            discovery_extra={"payload_press": "PRESS"},
        ))

    if cfg.screensaver:
        controls.append(Control(
            component="switch",
            slug="screensaver",
            name="Controls - Screensaver",
            icon="mdi:monitor-screenshot",
            handler=_handle_screensaver_set,
            own_state_topic=False,
            discovery_extra={
                # Reuse the system_state ticker's screensaver_running
                # binary so HA sees external start/stop transitions
                # (idle timer, hot corner) without us having to poll
                # again from the controls module.
                "_reuse_state_topic": "system/screensaver_running",
                "payload_on": "ON",
                "payload_off": "OFF",
                "state_on": "ON",
                "state_off": "OFF",
            },
        ))

    if cfg.sleep:  # destructive — opt-in
        controls.append(Control(
            component="button",
            slug="sleep",
            name="Controls - Sleep",
            icon="mdi:power-sleep",
            handler=_handle_sleep,
            discovery_extra={"payload_press": "PRESS"},
        ))

    if cfg.restart:  # destructive — opt-in
        controls.append(Control(
            component="button",
            slug="restart",
            name="Controls - Restart",
            icon="mdi:restart",
            handler=_handle_restart,
            discovery_extra={"payload_press": "PRESS"},
        ))

    if cfg.shutdown:  # destructive — opt-in
        controls.append(Control(
            component="button",
            slug="shutdown",
            name="Controls - Shut Down",
            icon="mdi:power",
            handler=_handle_shutdown,
            discovery_extra={"payload_press": "PRESS"},
        ))

    if cfg.display_notification:
        controls.append(Control(
            component="text",
            slug="display_notification",
            name="Controls - Display Notification",
            icon="mdi:bell-ring",
            handler=_handle_display_notification,
            discovery_extra={"max": 255},  # HA text entity hard cap
        ))

    if cfg.speak:
        controls.append(Control(
            component="text",
            slug="speak",
            name="Controls - Speak Text",
            icon="mdi:account-voice",
            handler=_handle_speak,
            discovery_extra={"max": 255},
        ))

    if cfg.open_url:
        controls.append(Control(
            component="text",
            slug="open_url",
            name="Controls - Open URL",
            icon="mdi:web",
            handler=_handle_open_url,
            discovery_extra={"max": 255},
        ))

    if cfg.media_controls:
        controls.append(Control(
            component="switch",
            slug="media_playing",
            name="Controls - Media Playing",
            icon="mdi:play-pause",
            handler=_handle_media_play_set,
            own_state_topic=False,
            discovery_extra={
                # Reuse the now_playing ticker's `state` topic; map
                # "Playing" -> ON and everything else (Paused / Stopped /
                # None) -> OFF. The 5s now_playing cadence makes the
                # toggle feel snappier than the 30s system_state default.
                "_reuse_state_topic": "now_playing/state",
                "value_template": "{{ 'ON' if value == 'Playing' else 'OFF' }}",
                "payload_on": "ON",
                "payload_off": "OFF",
                "state_on": "ON",
                "state_off": "OFF",
            },
        ))
        controls.append(Control(
            component="button",
            slug="media_next",
            name="Controls - Media Next",
            icon="mdi:skip-next",
            handler=_handle_media_next,
            discovery_extra={"payload_press": "PRESS"},
        ))
        controls.append(Control(
            component="button",
            slug="media_prev",
            name="Controls - Media Previous",
            icon="mdi:skip-previous",
            handler=_handle_media_prev,
            discovery_extra={"payload_press": "PRESS"},
        ))

    if cfg.caffeinate:
        controls.append(Control(
            component="switch",
            slug="caffeinate",
            name="Controls - Caffeinate",
            icon="mdi:coffee",
            handler=_handle_caffeinate_set,
            own_state_topic=True,
            discovery_extra={
                "payload_on": "ON",
                "payload_off": "OFF",
                "state_on": "ON",
                "state_off": "OFF",
            },
        ))

    return controls


# ---- ControlsHandler --------------------------------------------------------

class ControlsHandler:
    """Subscribes to MQTT command topics for each enabled control,
    publishes their HA discovery configs, and dispatches inbound
    commands to the per-control handler functions."""

    def __init__(
        self,
        cfg: Config,
        host_slug: str,
        publisher: MqttPublisher,
    ) -> None:
        self.cfg = cfg
        self.host_slug = host_slug
        self.publisher = publisher
        self.caffeinate = _CaffeinateManager()
        self.controls = _build_controls(cfg.controls)

    # ---- topic builders ----

    def _host_prefix(self) -> str:
        return f"{self.cfg.mqtt.topic_prefix}/{self.host_slug}"

    def command_topic(self, control: Control) -> str:
        # Per-component conventions: number/switch/select use /set,
        # buttons/text use /press / /send. We just use a single
        # per-control suffix to keep it simple.
        return f"{self._host_prefix()}/controls/{control.slug}/command"

    def state_topic(self, control: Control) -> str:
        return f"{self._host_prefix()}/controls/{control.slug}/state"

    def discovery_topic(self, control: Control) -> str:
        unique = f"macos_{self.host_slug}_{control.slug}"
        return f"{self.cfg.mqtt.discovery_prefix}/{control.component}/{unique}/config"

    # ---- discovery + subscription ----

    def publish_discovery(self) -> None:
        device = self.publisher.host_device_block()
        availability = self.cfg.mqtt.lwt_topic
        for control in self.controls:
            unique = f"macos_{self.host_slug}_{control.slug}"
            payload: dict[str, Any] = {
                "name": control.name,
                "unique_id": unique,
                "object_id": unique,
                "command_topic": self.command_topic(control),
                "icon": control.icon,
                "device": device,
                # Live entities: tie to LWT so HA flips Unavailable when
                # the bridge is offline rather than letting the user
                # press a button that goes nowhere.
                "availability_topic": availability,
                "payload_available": "online",
                "payload_not_available": "offline",
            }
            extra = dict(control.discovery_extra)
            # Resolve the special _reuse_state_topic placeholder into
            # the host-prefixed topic. Lets controls like volume/mute
            # piggyback on the system_state ticker's published values.
            reuse = extra.pop("_reuse_state_topic", None)
            if reuse is not None:
                payload["state_topic"] = f"{self._host_prefix()}/{reuse}"
            elif control.own_state_topic:
                payload["state_topic"] = self.state_topic(control)
            payload.update(extra)

            self.publisher.publish_discovery(
                component=control.component,
                unique_id=unique,
                payload=payload,
            )
        if self.controls:
            log.info(
                "controls: published discovery for %d entit%s: %s",
                len(self.controls),
                "y" if len(self.controls) == 1 else "ies",
                [c.slug for c in self.controls],
            )

    def subscribe(self) -> None:
        for control in self.controls:
            topic = self.command_topic(control)
            handler = self._make_dispatcher(control)
            self.publisher.subscribe(topic, handler)
        if self.controls:
            log.info(
                "controls: subscribed to %d command topic(s)", len(self.controls)
            )

    def _make_dispatcher(
        self, control: Control
    ) -> Callable[[str, bytes], None]:
        def dispatch(_topic: str, payload: bytes) -> None:
            try:
                state = control.handler(payload, self)
            except Exception:  # noqa: BLE001
                log.exception("controls: handler %s raised", control.slug)
                return
            # If the handler returned a state dict, publish it to the
            # per-control state topic so HA reflects the new state
            # immediately (used for caffeinate; volume/mute sync via
            # the system_state ticker instead).
            if state is None or not control.own_state_topic:
                return
            value = state.get("state")
            if value is not None:
                self.publisher.publish_state(self.state_topic(control), value)
        return dispatch
