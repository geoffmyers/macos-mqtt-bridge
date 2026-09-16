"""Currently-playing media (cross-app).

Best-effort detection of what's playing across the supported media
sources. Resolution order:

  1. ``nowplaying-cli`` if installed — single tool, covers any app
     that publishes via the macOS MediaRemote framework (Music,
     Spotify, Brave/Chrome with media key support, Apple Podcasts,
     etc.). Recommended: ``brew install nowplaying-cli``.
  2. AppleScript queries against Music.app and Spotify.app — only when
     the app is already running, so we don't accidentally launch one
     of them. Catches the most common cases on a Mac without the
     extra CLI tool.

Publishes:

  sensor.<host>_now_playing_state    — Playing | Paused | Stopped | None
  sensor.<host>_now_playing_track    — track title or "None"
  sensor.<host>_now_playing_artist
  sensor.<host>_now_playing_album
  sensor.<host>_now_playing_app      — friendly source app name
"""

from __future__ import annotations

import logging
import shutil
import subprocess
from typing import Any

from macos_bridge.mqtt import MqttPublisher, build_discovery_payload
from macos_bridge.phases.base import (
    AbstractTicker,
    screen_time_state_topic,
    screen_time_unique_id,
)

logger = logging.getLogger(__name__)


def _run(cmd: list[str], timeout: float = 2.0) -> str:
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout, check=False
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return ""
    return result.stdout if result.returncode == 0 else ""


def _is_app_running(app_name: str) -> bool:
    out = _run([
        "osascript", "-e",
        f'tell application "System Events" to (name of processes contains "{app_name}")',
    ])
    return out.strip().lower() == "true"


def _try_nowplaying_cli() -> dict[str, str] | None:
    """Returns ``{state, app, title, artist, album}`` from the
    ``nowplaying-cli`` shell tool, or None if unavailable."""
    if not shutil.which("nowplaying-cli"):
        return None
    state = _run(["nowplaying-cli", "get-raw"]).strip().lower()
    title = _run(["nowplaying-cli", "get", "title"]).strip()
    artist = _run(["nowplaying-cli", "get", "artist"]).strip()
    album = _run(["nowplaying-cli", "get", "album"]).strip()
    if not (title or artist):
        return {"state": "none", "app": "", "title": "", "artist": "", "album": ""}
    return {
        "state": "playing" if "play" in state else ("paused" if "pause" in state else "stopped"),
        "app": "",  # nowplaying-cli doesn't expose source app name
        "title": title,
        "artist": artist,
        "album": album,
    }


def _try_apple_music() -> dict[str, str] | None:
    if not _is_app_running("Music"):
        return None
    state = _run([
        "osascript", "-e",
        'tell application "Music" to player state as string',
    ]).strip().lower()
    if not state:
        return None
    if state == "stopped":
        return {"state": "stopped", "app": "Apple Music",
                "title": "", "artist": "", "album": ""}
    title = _run([
        "osascript", "-e",
        'tell application "Music" to name of current track',
    ]).strip()
    artist = _run([
        "osascript", "-e",
        'tell application "Music" to artist of current track',
    ]).strip()
    album = _run([
        "osascript", "-e",
        'tell application "Music" to album of current track',
    ]).strip()
    return {"state": state, "app": "Apple Music",
            "title": title, "artist": artist, "album": album}


def _try_spotify() -> dict[str, str] | None:
    if not _is_app_running("Spotify"):
        return None
    state = _run([
        "osascript", "-e",
        'tell application "Spotify" to player state as string',
    ]).strip().lower()
    if not state:
        return None
    if state == "stopped":
        return {"state": "stopped", "app": "Spotify",
                "title": "", "artist": "", "album": ""}
    title = _run([
        "osascript", "-e",
        'tell application "Spotify" to name of current track',
    ]).strip()
    artist = _run([
        "osascript", "-e",
        'tell application "Spotify" to artist of current track',
    ]).strip()
    album = _run([
        "osascript", "-e",
        'tell application "Spotify" to album of current track',
    ]).strip()
    return {"state": state, "app": "Spotify",
            "title": title, "artist": artist, "album": album}


def _detect() -> dict[str, str]:
    """Pick the best signal: nowplaying-cli first; if it returns nothing
    interesting (or isn't installed), try the per-app osascript probes.
    Apple Music wins ties over Spotify."""
    npc = _try_nowplaying_cli()
    if npc is not None and npc.get("title"):
        return npc
    for probe in (_try_apple_music, _try_spotify):
        result = probe()
        if result is not None and (result.get("title") or result["state"] == "playing"):
            return result
    if npc is not None:
        return npc
    return {"state": "none", "app": "", "title": "", "artist": "", "album": ""}


class NowPlayingTicker(AbstractTicker):
    name = "now_playing"

    def __init__(
        self,
        *,
        enabled: bool,
        interval_seconds: int,
        host_slug: str,
        topic_prefix: str,
        availability_topic: str,
    ) -> None:
        self._enabled = enabled
        self._interval_seconds = interval_seconds
        self._host_slug = host_slug
        self._topic_prefix = topic_prefix
        self._availability = availability_topic

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def interval_seconds(self) -> int | None:
        return self._interval_seconds

    def _state_topic(self, suffix: str) -> str:
        return screen_time_state_topic(
            self._topic_prefix, self._host_slug, f"now_playing/{suffix}"
        )

    def _unique_id(self, suffix: str) -> str:
        return screen_time_unique_id(self._host_slug, "now_playing", suffix)

    async def run_once(self, mqtt: MqttPublisher) -> None:
        info = _detect()
        # State values flow through internal comparisons as lowercase
        # ("playing"/"paused"/"stopped"/"none"); HA wants Title Case.
        display_state = info["state"].title() if info["state"] else "None"
        mqtt.publish_state(self._state_topic("state"), display_state)
        mqtt.publish_state(self._state_topic("app"), info["app"] or "None")
        mqtt.publish_state(self._state_topic("track"), info["title"] or "None")
        mqtt.publish_state(self._state_topic("artist"), info["artist"] or "None")
        mqtt.publish_state(self._state_topic("album"), info["album"] or "None")
        if info["state"] == "playing":
            logger.info(
                "now_playing: %s — %s / %s [%s]",
                info["app"], info["title"], info["artist"], info["album"],
            )

    async def publish_discovery(self, mqtt: MqttPublisher) -> None:
        device = mqtt.host_device_block()
        sensors = [
            ("state", "Now Playing - State", "mdi:play-pause"),
            ("app", "Now Playing - App", "mdi:application"),
            ("track", "Now Playing - Track", "mdi:music-note"),
            ("artist", "Now Playing - Artist", "mdi:account-music"),
            ("album", "Now Playing - Album", "mdi:album"),
        ]
        for suffix, name, icon in sensors:
            mqtt.publish_discovery(
                component="sensor",
                unique_id=self._unique_id(suffix),
                payload=build_discovery_payload(
                    name=name,
                    unique_id=self._unique_id(suffix),
                    state_topic=self._state_topic(suffix),
                    availability_topic=self._availability,
                    device=device,
                    icon=icon,
                ),
            )
