"""Unified bridge configuration schema.

Merges the formerly-separate configs from screen-time-ha-bridge and
macos-comms-mqtt-bridge into one pydantic model.

Layout:

  bridge:                  # logging, hostname slug, state path, paths
  mqtt:                    # one connection shared by all event sources + phases
  contacts:                # AddressBook resolver (used by comms event payloads)
  outbound:                # subscribe to messages/send and dispatch via osascript
  realtime_calls:          # spawn the Swift CXCallObserver subprocess
  sources:                 # event-driven SQLite watchers
    messages:
    calls:
    voicemail:
  aggregates / per_app / focused_app / family / activity / system_state /
                           host_metrics:   # state-snapshot tickers (knowledgeC.db + system probes)

The single ``MqttConfig`` carries the topic_prefix used to construct both
namespaces — comms event topics under ``<prefix>/<host>/comms/...`` and
phase ticker state topics flat under ``<prefix>/<host>/...`` — plus the LWT
topic and per-publish QoS / retain defaults.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from ha_mqtt_bridge import load_yaml_with_env
from ha_mqtt_bridge import slugify_hostname as _slugify_hostname
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


def _expand(p: str | None) -> str | None:
    if p is None:
        return None
    return os.path.expanduser(os.path.expandvars(p))


# ---- bridge / mqtt -------------------------------------------------------------


class BridgeSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    hostname: str | None = None
    log_level: Literal["debug", "info", "warning", "error"] = "info"
    log_path: str = "~/Library/Logs/macos-mqtt-bridge.log"
    state_path: str = (
        "~/Library/Application Support/macos-mqtt-bridge/state.json"
    )
    tmp_dir: str = "/tmp/macos-mqtt-bridge"
    poll_interval_seconds: int = Field(default=3, ge=1)

    @field_validator("hostname", mode="before")
    @classmethod
    def default_hostname(cls, v: str | None) -> str:
        return v or _slugify_hostname()


class MqttConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    host: str
    port: int = Field(default=8883, ge=1, le=65535)
    tls: bool = True
    ca_file: str | None = None
    username_env: str = "MQTT_USERNAME"
    password_env: str = "MQTT_PASSWORD"
    client_id: str = "macos-mqtt-bridge"
    topic_prefix: str = "macos"
    discovery_prefix: str = "homeassistant"
    keepalive: int = Field(default=60, ge=10)
    lwt_topic: str = "macos/status"
    lwt_online: str = "online"
    lwt_offline: str = "offline"

    # Quality of service per publish flavor.
    event_qos: Literal[0, 1, 2] = 1
    state_qos: Literal[0, 1, 2] = 0
    discovery_qos: Literal[0, 1, 2] = 1

    # Retain semantics:
    #   - events  : per-event point-in-time messages from comms sources.
    #               Default False so HA's last-value display reflects the
    #               state-mirror topic, not the raw event firehose.
    #   - state   : retained state-mirror topics from phase tickers AND
    #               the state-mirror topic comms emits per HA discovery
    #               entity. Default True so HA shows the last value after
    #               a bridge restart.
    retain_events: bool = False
    retain_state: bool = True

    # Toggles whether comms event sources also publish HA discovery configs.
    # Phase tickers always publish discovery (they're the entire point).
    discovery_enabled: bool = True

    @model_validator(mode="after")
    def _tls_requires_ca_file(self) -> MqttConfig:
        if self.tls and not self.ca_file:
            raise ValueError("ca_file is required when tls=true")
        return self


# ---- comms event sources -------------------------------------------------------


class MessagesSource(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    db_path: str = "~/Library/Messages/chat.db"
    include_text: bool = True
    include_attributed_body_fallback: bool = True


class CallsSource(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    db_path: str = (
        "~/Library/Application Support/CallHistoryDB/CallHistory.storedata"
    )
    emit_started_event: bool = True
    facetime_service_substrings: list[str] = Field(
        default_factory=lambda: ["facetime", "avconference"]
    )
    phone_service_substrings: list[str] = Field(default_factory=lambda: ["telephony"])


class VoicemailSource(BaseModel):
    """macOS 26: voicemails live in the FaceTime/Phone shared message store
    (no ~/Library/Voicemail/ on this OS). Audio files in
    Assets/<UUID[0:2]>/<UUID>.{amr,m4a,...}."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    db_path: str = (
        "~/Library/Group Containers/group.com.apple.FaceTime/"
        "com.apple.facetimemessagestored/Data Store/FaceTimeMessageStore-local.sqlitedb"
    )
    assets_dir: str = (
        "~/Library/Group Containers/group.com.apple.FaceTime/"
        "com.apple.facetimemessagestored/Data Store/Assets"
    )
    audio_extensions: list[str] = Field(
        default_factory=lambda: ["amr", "m4a", "caf", "wav", "mp3", "aac"]
    )
    include_audio_path: bool = True
    include_deleted: bool = False
    facetime_provider_substrings: list[str] = Field(
        default_factory=lambda: ["facetime"]
    )
    phone_provider_substrings: list[str] = Field(
        default_factory=lambda: ["coretelephony"]
    )


class SourcesSection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    messages: MessagesSource = Field(default_factory=MessagesSource)
    calls: CallsSource = Field(default_factory=CallsSource)
    voicemail: VoicemailSource = Field(default_factory=VoicemailSource)


class OutboundSection(BaseModel):
    """Outbound: subscribe to <prefix>/<host>/comms/messages/send and run
    osascript against Messages.app's scripting dictionary."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    osascript_path: str = "osascript"


class RealtimeCallsSection(BaseModel):
    """Spawn the Swift CXCallObserver helper to emit real-time phone state
    transitions (ringing, connected, ended)."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    binary_path: str = "./helpers/call-observer/.build/release/CallObserver"


class ContactsSection(BaseModel):
    """Resolves phone numbers and emails against the macOS AddressBook so
    payloads can include first/last name, organization, label, contact UID,
    and an optional base64-encoded profile photo."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    address_book_dir: str = "~/Library/Application Support/AddressBook"
    include_photo: bool = True
    photo_field: Literal["thumbnail", "full", "thumbnail_or_full"] = "thumbnail"


# ---- phase ticker config (screen-time half) ------------------------------------


class PhaseSourcesConfig(BaseModel):
    """Paths to the SQLite databases the phase tickers read from."""

    model_config = ConfigDict(extra="forbid")

    knowledge_db: str = "~/Library/Application Support/Knowledge/knowledgeC.db"
    rm_admin_local: str = (
        "${DARWIN_USER_DIR}/com.apple.ScreenTimeAgent/Store/RMAdminStore-Local.sqlite"
    )
    rm_admin_cloud: str = (
        "${DARWIN_USER_DIR}/com.apple.ScreenTimeAgent/Store/RMAdminStore-Cloud.sqlite"
    )


class AggregatesConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    interval_seconds: int = Field(default=900, ge=1)


class AppEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    bundle_id: str
    friendly_name: str | None = None
    icon: str | None = None


class PerAppConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    interval_seconds: int = Field(default=60, ge=1)
    apps: list[AppEntry] = Field(default_factory=list)


class FocusedAppConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    poll_interval_seconds: int = Field(default=3, ge=1)
    publish_locked_state: bool = True


class BraveHistoryConfig(BaseModel):
    """Brave Browser per-URL daily dwell-time from the Chromium ``History``
    SQLite DB under ``~/Library/Application Support/BraveSoftware/Brave-Browser``.

    Disabled by default — opt in per host. Publishes one retained JSON blob
    per profile to ``<prefix>/<host>/today/web/<profile>`` for any consumer
    that wants per-visit browsing time. Reads a WAL-safe snapshot copy
    (never the live DB), so it is safe to run while Brave is open.
    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    interval_seconds: int = Field(default=900, ge=60)
    # Override the Brave Application Support directory (default: the standard
    # per-user location resolved inside the ticker). Useful for non-default
    # installs or for pointing tests at a fixture profile tree.
    brave_home: str | None = None
    # Cap individual visits published per profile per tick. Each of today's
    # page views (with its own start timestamp + dwell) is emitted, so a heavy
    # browsing day can be large; the cap bounds the retained JSON payload.
    max_visits_per_profile: int = Field(default=5000, ge=1)


class FamilyMember(BaseModel):
    model_config = ConfigDict(extra="forbid")

    dsid: int
    slug: str


class FamilyConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    is_family_organizer: bool = False
    interval_seconds: int = Field(default=300, ge=60)
    members: list[FamilyMember] = Field(default_factory=list)

    @model_validator(mode="after")
    def _enabled_requires_organizer(self) -> FamilyConfig:
        if self.enabled and not self.is_family_organizer:
            raise ValueError("family.enabled requires is_family_organizer=true")
        return self


class ActivityConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    interval_seconds: int = Field(default=5, ge=1)
    input_active_threshold_seconds: int = Field(default=5, ge=1)
    camera_log_window_seconds: int = Field(default=10, ge=2)
    microphone_log_window_seconds: int = Field(default=10, ge=2)


class SystemStateConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    interval_seconds: int = Field(default=30, ge=5)
    wifi_interface: str = "en0"


class HostMetricsConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    interval_seconds: int = Field(default=30, ge=5)
    disk_mount: str = "/System/Volumes/Data"


class TodayCountersConfig(BaseModel):
    """Today-so-far counters for the comms event sources (messages received,
    messages sent, phone calls, FaceTime calls, voicemails). Resets at local
    midnight by virtue of always querying ``start_of_today_mat`` at tick time."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    interval_seconds: int = Field(default=60, ge=5)


class VirtualMeetingsConfig(BaseModel):
    """Detect and track virtual meetings (Zoom / Teams / FaceTime / browser
    Google Meet) by inspecting per-process power assertions. The meeting
    app does NOT need to be foregrounded for detection."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    interval_seconds: int = Field(default=5, ge=1)
    # When detection drops, wait this many consecutive negative ticks before
    # declaring the meeting ended. Smooths over momentary pmset gaps.
    end_grace_ticks: int = Field(default=2, ge=1)


class PermissionsConfig(BaseModel):
    """Probe and report which macOS TCC permissions the bridge currently
    holds (FDA, Accessibility, Automation for Messages, Location Services).
    Slow cadence — these only change when the user toggles them in System
    Settings."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    interval_seconds: int = Field(default=300, ge=30)


class FocusModeConfig(BaseModel):
    """macOS Focus mode (DND / Work / Sleep / etc.). Reads the user's
    DoNotDisturb assertions store; no shell call, very cheap."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    interval_seconds: int = Field(default=10, ge=1)


class UnreadMessagesConfig(BaseModel):
    """Counts unread incoming iMessage/SMS rows from chat.db. Cheap query."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    interval_seconds: int = Field(default=30, ge=5)


class SoftwareUpdatesConfig(BaseModel):
    """Pending macOS updates from ``softwareupdate -l --no-scan``. Fast
    cached read, but very slow cadence makes sense — pending updates
    don't change minute-to-minute."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    interval_seconds: int = Field(default=3600, ge=300)


class TailscaleConfig(BaseModel):
    """Tailscale connectivity from ``tailscale status --json``. Skips
    silently when the CLI isn't on PATH."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    interval_seconds: int = Field(default=60, ge=5)


class TimeMachineConfig(BaseModel):
    """Time Machine backup state from ``tmutil status`` and
    ``tmutil latestbackup``. Fast commands, medium cadence."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    interval_seconds: int = Field(default=300, ge=30)


class DisplaysConfig(BaseModel):
    """Connected display enumeration from ``system_profiler SPDisplaysDataType``.
    Moderately slow command (~1-3s warm), so default cadence is 30s."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    interval_seconds: int = Field(default=30, ge=10)


class SecurityPostureConfig(BaseModel):
    """FileVault, Application Firewall, and SIP status. Slow-changing
    flags — 5min cadence is plenty."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    interval_seconds: int = Field(default=300, ge=60)


class SystemInfoConfig(BaseModel):
    """Static macOS / hardware identity diagnostics — OS version + build,
    model, serial, chip, memory, storage capacity. Slow cadence; values
    only change across an OS upgrade or hardware swap."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    interval_seconds: int = Field(default=3600, ge=60)


class NowPlayingConfig(BaseModel):
    """Currently-playing media via ``nowplaying-cli`` if installed, else
    AppleScript probes against Music.app and Spotify.app (only when those
    apps are already running, to avoid launching them just to check)."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    interval_seconds: int = Field(default=5, ge=1)


class ControlsConfig(BaseModel):
    """Inbound HA controls — buttons, switches, numbers, selects, text
    inputs that turn into shell calls on this Mac.

    Per-control flags let the user enable / disable each one. Destructive
    actions (sleep / restart / shutdown) default to ``False`` so a
    compromised broker can't trivially nuke the machine; the user opts
    in by flipping the flag in config.yaml.
    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool = True

    # Audio
    volume: bool = True
    mute: bool = True

    # Display / session
    lock_screen: bool = True
    screensaver: bool = True
    caffeinate: bool = True

    # Notifications / TTS / URL — read-only side effects
    display_notification: bool = True
    speak: bool = True
    open_url: bool = True

    # Media controls (uses nowplaying-cli if installed, falls back to
    # AppleScript Music.app commands).
    media_controls: bool = True

    # DESTRUCTIVE — opt-in. Anyone able to publish to the broker can
    # use these to take the machine down, so they're off by default.
    sleep: bool = False
    restart: bool = False
    shutdown: bool = False


class AudioHijackConfig(BaseModel):
    """Automatic Audio Hijack session control.

    Each *_session field must exactly match a session name in Audio Hijack.
    Set null / omit to skip recording for that trigger.

    Audio Hijack must be running — the bridge does not launch it.
    auto_stop: only stop sessions the bridge started, not ones you started
    manually from Audio Hijack's UI.
    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    zoom_session: str | None = None
    teams_session: str | None = None
    facetime_session: str | None = None
    browser_session: str | None = None
    phone_session: str | None = None
    auto_stop: bool = True


class LocationConfig(BaseModel):
    """CoreLocation-derived sensors via the LocationFetcher Swift helper.

    Spawns the helper once per tick, parses its JSON output, publishes
    lat/lon/altitude/accuracy + reverse-geocoded address. Apple
    rate-limits CLGeocoder to ~50/hour so the default cadence is 5
    minutes; tighter cadences risk geocode throttling. The helper
    binary needs Location Services TCC permission — granted once via
    System Settings or by running it interactively to trigger the
    prompt."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    interval_seconds: int = Field(default=300, ge=30)
    binary_path: str = (
        "./helpers/location-fetcher/.build/release/"
        "LocationFetcher.app/Contents/MacOS/LocationFetcher"
    )
    # Per-spawn timeout passed to the helper's --timeout flag.
    fetch_timeout_seconds: int = Field(default=20, ge=5, le=120)

    # Home zone for computing the device_tracker state. HA's MQTT
    # device_tracker integration requires the published state to be one
    # of ``home`` / ``not_home`` / a known zone name — it does NOT
    # auto-resolve coordinates against HA zones the way mobile_app does.
    # When ``home_latitude`` and ``home_longitude`` are set, the bridge
    # publishes ``home`` if the most recent CoreLocation fix is within
    # ``home_radius_meters``, else ``not_home``. When unset, the bridge
    # falls back to publishing ``home`` always — fine for a desk-bound
    # Mac that doesn't move, wrong for a laptop you carry around.
    home_latitude: float | None = None
    home_longitude: float | None = None
    home_radius_meters: float = Field(default=100.0, ge=10.0)


# ---- top-level config ---------------------------------------------------------


class Config(BaseModel):
    model_config = ConfigDict(extra="forbid")

    bridge: BridgeSettings = Field(default_factory=BridgeSettings)
    mqtt: MqttConfig

    contacts: ContactsSection = Field(default_factory=ContactsSection)
    outbound: OutboundSection = Field(default_factory=OutboundSection)
    realtime_calls: RealtimeCallsSection = Field(default_factory=RealtimeCallsSection)

    sources: SourcesSection = Field(default_factory=SourcesSection)
    phase_sources: PhaseSourcesConfig = Field(default_factory=PhaseSourcesConfig)

    aggregates: AggregatesConfig = Field(default_factory=AggregatesConfig)
    per_app: PerAppConfig = Field(default_factory=PerAppConfig)
    focused_app: FocusedAppConfig = Field(default_factory=FocusedAppConfig)
    brave_history: BraveHistoryConfig = Field(default_factory=BraveHistoryConfig)
    family: FamilyConfig = Field(default_factory=FamilyConfig)
    activity: ActivityConfig = Field(default_factory=ActivityConfig)
    system_state: SystemStateConfig = Field(default_factory=SystemStateConfig)
    host_metrics: HostMetricsConfig = Field(default_factory=HostMetricsConfig)
    today_counters: TodayCountersConfig = Field(default_factory=TodayCountersConfig)
    virtual_meetings: VirtualMeetingsConfig = Field(default_factory=VirtualMeetingsConfig)
    permissions: PermissionsConfig = Field(default_factory=PermissionsConfig)
    focus_mode: FocusModeConfig = Field(default_factory=FocusModeConfig)
    unread_messages: UnreadMessagesConfig = Field(default_factory=UnreadMessagesConfig)
    software_updates: SoftwareUpdatesConfig = Field(default_factory=SoftwareUpdatesConfig)
    tailscale: TailscaleConfig = Field(default_factory=TailscaleConfig)
    time_machine: TimeMachineConfig = Field(default_factory=TimeMachineConfig)
    displays: DisplaysConfig = Field(default_factory=DisplaysConfig)
    security_posture: SecurityPostureConfig = Field(default_factory=SecurityPostureConfig)
    system_info: SystemInfoConfig = Field(default_factory=SystemInfoConfig)
    now_playing: NowPlayingConfig = Field(default_factory=NowPlayingConfig)
    location: LocationConfig = Field(default_factory=LocationConfig)
    controls: ControlsConfig = Field(default_factory=ControlsConfig)
    audio_hijack: AudioHijackConfig = Field(default_factory=AudioHijackConfig)

    def expanded_paths(self) -> dict[str, str | None]:
        return {
            "log_path": _expand(self.bridge.log_path),
            "state_path": _expand(self.bridge.state_path),
            "tmp_dir": _expand(self.bridge.tmp_dir),
            "messages_db": _expand(self.sources.messages.db_path),
            "calls_db": _expand(self.sources.calls.db_path),
            "voicemail_db": _expand(self.sources.voicemail.db_path),
            "voicemail_assets_dir": _expand(self.sources.voicemail.assets_dir),
            "address_book_dir": _expand(self.contacts.address_book_dir),
            "ca_file": _expand(self.mqtt.ca_file),
            "knowledge_db": _expand(self.phase_sources.knowledge_db),
            "rm_admin_local": _expand(self.phase_sources.rm_admin_local),
            "rm_admin_cloud": _expand(self.phase_sources.rm_admin_cloud),
            "brave_home": _expand(self.brave_history.brave_home),
        }


@dataclass(frozen=True)
class LoadedCredentials:
    """Resolved MQTT credentials read from env at startup."""

    username: str
    password: str


def load_config(path: str | Path) -> Config:
    """Read, env-substitute, and validate the YAML config.

    Delegates the YAML read + ``${ENV_VAR}`` substitution to the shared
    toolkit's ``load_yaml_with_env``; pydantic validation stays here
    because the ``Config`` schema is macOS-specific.
    """
    data = load_yaml_with_env(path)
    return Config.model_validate(data)


def load_config_with_credentials(path: str | Path) -> tuple[Config, LoadedCredentials]:
    """Load config and resolve MQTT credentials from env. Raises KeyError if
    the env vars referenced by mqtt.username_env / password_env are missing.
    """
    cfg = load_config(path)
    username_env = cfg.mqtt.username_env
    password_env = cfg.mqtt.password_env
    if username_env not in os.environ:
        raise KeyError(f"environment variable {username_env} is not set")
    if password_env not in os.environ:
        raise KeyError(f"environment variable {password_env} is not set")
    creds = LoadedCredentials(
        username=os.environ[username_env],
        password=os.environ[password_env],
    )
    return cfg, creds
