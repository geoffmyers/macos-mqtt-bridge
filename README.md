<p align="center">
  <img src="docs/icon.svg" width="96" height="96" alt="macOS MQTT Bridge icon">
</p>

# macOS MQTT Bridge

<!-- BADGES:START -->
![Python 3.12+](https://img.shields.io/badge/Python-3.12+-3776ab?style=flat-square&logo=python)
[![Licence GPL-3.0-or-later](https://img.shields.io/badge/licence-GPL--3.0--or--later-blue?style=flat-square)](LICENSE.md)
[![PRs welcome](https://img.shields.io/badge/PRs-welcome-brightgreen?style=flat-square)](CONTRIBUTING.md)
<!-- BADGES:END -->

## Table of Contents

- [Description](#description)
- [Features](#features)
- [Requirements](#requirements)
- [Installation](#installation)
- [Usage](#usage)
- [Configuration](#configuration)
- [Home Assistant](#home-assistant)
- [Permissions](#permissions)
- [Architecture](#architecture)
- [Credits](#credits)
- [Contributing](#contributing)
- [License](#license)

## Description

A single background daemon (`launchd`-supervised) that turns a Mac into a
Home Assistant device: it watches Messages, Phone, FaceTime and voicemail
history and publishes each new event in real time, and it runs 21
independently-scheduled tickers that snapshot everything from screen-time
and focused-app to Wi-Fi, battery, Focus mode, Time Machine status and
currently-playing media. Everything is published over MQTT with Home
Assistant MQTT discovery, so every sensor shows up automatically as one
"macOS Bridge" device — no YAML to write on the Home Assistant side.

It is built for a Mac you're signed in to and physically own; every data
source it reads (Messages, call history, Screen Time, system state) is local
to that machine, and every entity it publishes is scoped to that one Mac.

## Features

- **Real-time comms events** — Messages (sent/received/reactions/edits/
  retractions/group changes), phone calls, FaceTime calls (including
  live ringing/connected/ended via a `CXCallObserver` subprocess, not just
  after-the-fact history), and voicemails with transcript + audio path —
  each becomes one MQTT event the moment it happens, enriched with the
  caller/sender's name, label and photo from AddressBook.
- **Outbound messages.** Subscribe to a topic and send an iMessage/SMS
  through Messages.app from a Home Assistant automation.
- **21 independently-paced state tickers** covering screen time (daily
  totals, per-app usage from a curated allow-list, per-visit Brave browsing
  history), the currently-focused app, Wi-Fi/Bluetooth/battery/brightness,
  Focus mode, unread message count, pending software updates, Tailscale
  connectivity, Time Machine status, external displays, FileVault/Firewall/
  SIP status, currently-playing media, camera/mic/input activity, the
  Mac's hardware and OS identity, and a
  one-shot-per-fix location `device_tracker` with reverse-geocoded address.
  See [Configuration](#configuration) for the full list.
- **Inbound controls.** 14 Home Assistant entities that drive the Mac back:
  volume, mute, lock screen, screensaver, caffeinate, a notification/TTS/
  URL-opener, media transport, and (off by default) sleep/restart/shutdown.
- **Survives a broker outage.** Comms events queue to a disk-backed outbox
  and drain in order once the broker comes back, instead of being dropped.
- **Contact photos as first-class HA image entities**, not inlined base64 —
  8 `image` entities carry the last contact photo per comms category
  instead of bloating every event payload.
- **Automatic error reporting** (opt-in, off by default) — see
  [Permissions](#permissions).

## Requirements

- **macOS**, Apple Silicon or Intel. Developed and run on recent macOS
  releases; the comms sources track Apple's own schema changes (e.g. the
  macOS 26 merge of the voicemail store into the shared FaceTime/Phone
  message database — see `config.example.yaml`).
- **Python 3.12+** and [`uv`](https://docs.astral.sh/uv/) (installer script
  uses `uv`; a plain `venv` + `pip` works too, see
  [CONTRIBUTING.md](CONTRIBUTING.md)).
- **Xcode Command Line Tools** (`swiftc`, `codesign`) to build the two small
  Swift helpers — a Mac-Catalyst `CXCallObserver` subprocess for real-time
  call state, and a `CoreLocation` one-shot fetcher.
- An MQTT broker with Home Assistant's [MQTT integration](https://www.home-assistant.io/integrations/mqtt/)
  configured, and MQTT discovery enabled (the default).
- See [Permissions](#permissions) for the macOS privacy grants (Full Disk
  Access, Accessibility, Automation, Location Services) each feature needs.

## Installation

```bash
git clone https://github.com/geoffmyers/macos-mqtt-bridge.git
cd macos-mqtt-bridge

cp config.example.yaml config.yaml
$EDITOR config.yaml   # at minimum, set mqtt.host

cp .env.example .env
$EDITOR .env          # MQTT_USERNAME and MQTT_PASSWORD
chmod 600 .env

./install.sh
```

`install.sh` creates a `uv`-managed virtual environment, installs the shared
toolkit, builds and ad-hoc-signs both Swift helpers, primes the comms
sources' state so the first run doesn't replay your entire message/call
history, installs a `launchd` LaunchAgent, and prints which System
Settings → Privacy & Security grants you still need (see
[Permissions](#permissions)). Re-run `./install.sh` any time after pulling an
update; `./install.sh --uninstall` removes just the LaunchAgent (your
`config.yaml`/`.env` are left in place).

## Usage

Once installed, the bridge runs continuously as a LaunchAgent — there is
nothing to keep in a terminal. A few commands are useful directly:

```bash
# Single dry-run poll: print what would be published, without touching MQTT.
.venv/bin/python -m macos_bridge --config config.yaml dump-once

# Print the top-N most-used apps from the last N days as a YAML block,
# ready to paste under per_app.apps in config.yaml.
.venv/bin/python -m macos_bridge --config config.yaml bootstrap-allowlist --top 20 --days 30

# Watch the logs.
tail -f ~/Library/Logs/macos-mqtt-bridge.log
```

## Configuration

Everything lives in `config.yaml` (copied from `config.example.yaml`, which
documents every key inline); broker credentials come from environment
variables so they're never written into the file. The top-level sections:

| Section | Publishes |
|---|---|
| `bridge` | Hostname slug, log path, state path — not itself a data source. |
| `mqtt` | Broker connection, topic prefix, QoS/retain policy. |
| `contacts` | AddressBook enrichment (name/label/photo) for comms events — not a topic of its own. |
| `outbound` | Subscribes to `comms/messages/send`; drives Messages.app. |
| `realtime_calls` | Live ringing/connected/ended call state via the Swift `CallObserver` helper. |
| `sources.messages` / `.calls` / `.voicemail` | The three comms event sources. |
| `aggregates` | Daily totals: screen time, pickups, top app + its hours. |
| `per_app` | Per-app daily screen-time minutes for a curated app allow-list. |
| `focused_app` | Live foreground app (bundle ID; window title/document with Accessibility). |
| `brave_history` | Per-visit Brave browsing history, one retained JSON blob per profile. |
| `family` | Synced Family Sharing Screen Time for other members — **disabled by default**. |
| `activity` | Camera/mic/audio/input-idle binary sensors. |
| `system_state` | Wi-Fi SSID, Bluetooth, brightness, volume, lid, battery. |
| `host_metrics` | CPU, memory, disk, load, boot time. |
| `today_counters` | Today's message/call/FaceTime/voicemail counts. |
| `virtual_meetings` | Zoom/Teams/FaceTime/Meet-in-browser detection via `pmset` assertions. |
| `permissions` | Reports which of the bridge's own macOS TCC grants are held/denied. |
| `focus_mode` | Current Focus/DND mode. |
| `unread_messages` | Unread iMessage/SMS count, with a per-chat breakdown. |
| `software_updates` | Pending macOS update count. |
| `tailscale` | Tailscale connectivity (skips silently if the CLI isn't installed). |
| `time_machine` | Backup status and most recent backup time. |
| `displays` | External display enumeration ("am I docked"). |
| `security_posture` | FileVault / Application Firewall / SIP enabled flags. |
| `system_info` | What the Mac is: macOS version and build, kernel, model, serial number, hardware UUID, computer name, chip, cores, memory, storage (hourly). |
| `now_playing` | Currently-playing media (`nowplaying-cli` if installed, else Music.app/Spotify.app AppleScript probes). |
| `controls` | The 14 inbound HA control entities — see [Features](#features). |
| `location` | One-shot-per-fix `device_tracker` with reverse-geocoded address; `home_latitude`/`home_longitude`/`home_radius_meters` compute home/away. |
| `audio_hijack` | Auto start/stop [Audio Hijack](https://rogueamoeba.com/audiohijack/) recording sessions on meeting/call start/end. |

Each section has its own `enabled` flag and (where applicable)
`interval_seconds`; see `config.example.yaml` for every default and a
comment on what each one does.

## Home Assistant

MQTT discovery means nothing needs configuring on the Home Assistant side —
every entity below appears automatically the first time the bridge
publishes it, grouped under one **"macOS Bridge — `<hostname>`"** device:

| Group | Entities |
|---|---|
| Messages / Phone / FaceTime / Voicemail | ~25 derived sensors (last sender/timestamp/duration/direction/transcript, truncated to fit HA's 255-char state cap) + 8 `image` entities for the last contact photo per category + retained `state/last_*` mirrors seeded from history at startup |
| Screen time | Today's total, pickups, top app + hours, per-app minutes for the configured allow-list, per-visit Brave browsing seconds |
| Live state | Focused app, Focus mode, virtual-meeting in-progress, now-playing media, Wi-Fi SSID, Bluetooth, battery, brightness, volume/mute |
| Activity | Camera/mic/audio-in-use and input-idle binary sensors |
| System / diagnostic | Host metrics (CPU/mem/disk/load), granted/denied permissions, FileVault/Firewall/SIP, pending software updates, Time Machine status, Tailscale connectivity, external displays — tagged `entity_category: diagnostic` |
| Location | One `device_tracker` (GPS `source_type`) with `home`/`not_home`/zone resolution and a reverse-geocoded address in its attributes |
| Controls | Number `Volume`; switches `Mute` / `Caffeinate` / `Screensaver` / `Media Playing`; buttons `Lock Screen` / `Media Next` / `Media Previous`; text inputs `Display Notification` / `Speak Text` / `Open URL`; opt-in buttons `Sleep` / `Restart` / `Shut Down` |

Live sensors carry an availability topic so Home Assistant shows them
`Unavailable` when the bridge is offline; historical "last event" sensors
deliberately don't, so they keep showing their last known value instead.

## Permissions

macOS gates each data source behind its own privacy grant, in
**System Settings → Privacy & Security**:

| Grant | Why | Without it |
|---|---|---|
| **Full Disk Access** (for the bridge's Python interpreter, `.venv/bin/python`) | Reading `chat.db`, `CallHistory.storedata`, the FaceTime/Phone voicemail store, `knowledgeC.db`, the Screen Time `RMAdminStore` files, and the AddressBook source databases all live under a protected location. | The corresponding comms/screen-time sources silently return nothing; the `permissions` ticker's sensor tells you which grant is missing. |
| **Accessibility** | Window title and open-document (`AXDocument`) extras on the focused-app sensor. | The bundle ID still publishes; only the title/document fields are empty. |
| **Automation → Messages** | Outbound sending (`osascript` driving Messages.app's scripting dictionary). | `comms/messages/send` requests fail; nothing else is affected. |
| **Location Services**, granted to the `LocationFetcher.app` helper (not the daemon itself) | The location ticker's one-shot `CoreLocation` fix. The helper is ad-hoc signed with its own embedded `Info.plist` specifically so this grant survives a `.venv` rebuild. | The location `device_tracker` doesn't update; `wifi_ssid` reports a self-explanatory placeholder instead of macOS's `<redacted>` (which system_profiler substitutes when Location Services is denied to the caller). |

**Automatic error reporting is off by default.** If you set both
`GITHUB_ERROR_TOKEN` and `GITHUB_REPO` in `.env`, unhandled exceptions are
reported via a GitHub `repository_dispatch` call to the repo you name —
useful if you maintain a fork and want a paper trail of production crashes.
Leave both unset (the default) and nothing is ever sent anywhere; see
`_shared/python-github-error-reporter/`.

## Architecture

Comms events (Messages/Phone/FaceTime/Voicemail) and 21 independently-paced
state tickers share one MQTT connection and one Home Assistant device block:

```
chat.db, CallHistory.storedata, ──┐
FaceTime voicemail store          ├─► comms sources ─► disk-backed outbox ─┐
CXCallObserver (Swift subprocess) ─┘   (poll loop, daemon thread)          │
                                                                            ├─► MQTT ─► Home Assistant
knowledgeC.db, system_profiler,   ──► 21 phase tickers                     │
pmset, ioreg, CoreLocation, ...       (independent asyncio tasks) ─────────┘
```

See [ARCHITECTURE.md](ARCHITECTURE.md) for the topic-namespace scheme, the
phase-ticker pattern, why call observation needs a compiled Swift helper,
and the full file layout.

## Credits

- Built on `_shared/ha-mqtt-bridge-toolkit/` and
  `_shared/python-github-error-reporter/` (both vendored into this
  repository at publish time), shared libraries used across this author's
  home-automation bridges.
- MQTT via [`paho-mqtt`](https://eclipse.dev/paho/index.php?page=clients/python/index.php).
- Config validation via [`pydantic`](https://docs.pydantic.dev/).
- The README icon is the [Font Awesome](https://fontawesome.com/) `laptop`
  glyph, used under [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/).
- Apple, macOS, Messages, FaceTime and Focus are trademarks of Apple Inc.
  This project is not affiliated with, endorsed by, or sponsored by Apple.

Written by Geoff Myers.

## Contributing

Bug reports and pull requests are welcome. See [CONTRIBUTING.md](CONTRIBUTING.md)
for setup, checks and how this repository is published.

## License

This program is free software: you can redistribute it and/or modify it under
the terms of the GNU General Public License as published by the Free Software
Foundation, either version 3 of the License, or (at your option) any later
version.

This program is distributed in the hope that it will be useful, but WITHOUT ANY
WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS FOR A
PARTICULAR PURPOSE. See [LICENSE.md](LICENSE.md) for the full text of the GNU
General Public License.

SPDX-License-Identifier: `GPL-3.0-or-later`
