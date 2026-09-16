# Architecture

## Overview

One process, one MQTT connection, two cooperating halves that share the
connection and the Home Assistant device block but are otherwise independent:

```
                          ┌─────────────────────────────────────────┐
                          │              Bridge (runtime.py)          │
                          │                                           │
 chat.db ───────┐         │  ┌─────────────────────────────────────┐  │
 CallHistory.db ─┼─ poll ─┼─►│  Comms half (daemon thread)          │  │
 FaceTime store ─┘  loop  │  │  Messages / Calls / Voicemail        │──┼──► MQTT
                          │  │  sources → events → Outbox           │  │  (ThreadedPublisher,
 CXCallObserver ─────────►│  │  (disk-backed, survives a broker      │  │   one LWT, one
 (Swift subprocess)       │  │   outage) → publish                  │  │   device block)
                          │  └─────────────────────────────────────┘  │
 knowledgeC.db, ─────────►│  ┌─────────────────────────────────────┐  │
 system_profiler,         │  │  Phase tickers (asyncio tasks)       │  │
 pmset, ioreg, ...        │  │  21 independent cadenced tickers,    │──┘
                          │  │  each its own asyncio task           │
                          │  └─────────────────────────────────────┘  │
                          └─────────────────────────────────────────┘
```

- **The comms half** watches three local databases that macOS itself already
  maintains (Messages, Phone/FaceTime call history, and voicemail) for new
  rows, and turns each new row into one MQTT event. It runs in its own daemon
  thread so a slow SQLite read never blocks a phase ticker's asyncio loop.
- **The phase tickers** are 21 independent, differently-paced state-snapshot
  publishers — from "how much screen time today" to "is the lid closed" — each
  its own `asyncio` task so a slow one (a `softwareupdate -l` scan, say)
  doesn't stall a fast one (the 3-second focused-app poll).
- **A Swift subprocess** (`helpers/call-observer/`) streams call state changes
  in real time via `CXCallObserver`, a capability only available to a
  Mac-Catalyst-built binary, not to Python.
- Both halves publish through the same `MqttPublisher` (a thin subclass of
  `ha_mqtt_bridge.ThreadedPublisher`) and share one Home Assistant device
  block, so every sensor this bridge creates — comms or phase — groups under
  one "macOS Bridge — `<hostname>`" device in Home Assistant.

## Two topic namespaces, one prefix

Every topic lives under `<mqtt.topic_prefix>/<host_slug>/...` (default prefix
`macos`). Two shapes coexist under that root, chosen so they can never
collide:

| Namespace | Shape | Used by |
|---|---|---|
| Comms events | `<prefix>/<host>/comms/<source>/<event>` | Messages, Calls, FaceTime, Voicemail — point-in-time events, plus retained `state/last_*` mirrors seeded from history at startup |
| Phase state | `<prefix>/<host>/<suffix>` (flat, no `comms/`) | The 21 phase tickers — retained state-mirror topics |

`host_slug` defaults to a slugified `hostname -s` (override with
`bridge.hostname` in config) so multiple Macs can publish to the same broker
without colliding.

## The comms half: poll → event → outbox → publish

`sources/messages.py`, `sources/calls.py`, `sources/voicemail.py` each poll
their SQLite database (Messages' `chat.db`, `CallHistory.storedata`, and
macOS 26's merged FaceTime/Phone voicemail store) on `bridge.poll_interval_seconds`,
looking for rows newer than the last-seen high-water mark persisted in
`state.json`. A new row becomes an event dict, which the comms thread hands to
a disk-backed `Outbox` (from `ha-mqtt-bridge-toolkit`) before it ever touches
the network — so a broker outage queues events to disk instead of dropping
them, and a bridge restart mid-outage resumes the queue rather than
replaying or losing history.

Real-time call state (`ringing`/`connected`/`ended` as they happen, not just
after the fact from `CallHistory.storedata`) comes from a separate path: a
small Mac-Catalyst Swift binary (`helpers/call-observer/`) subscribes to
`CXCallObserver` and streams JSON lines to the bridge over its stdout, which
`realtime_calls.py` reads and re-emits through the same event pipeline.
`CXCallObserver` is a CallKit API with no equivalent Python or shell surface,
which is why this one piece is compiled Swift rather than another Python
module.

`contacts.py`'s `ContactResolver` enriches every comms event with a name,
label, and (optionally) a contact photo, indexed at startup from the local
AddressBook source databases (`discover_source_dbs()` finds every
`AddressBook-v22.abcddb` under `~/Library/Application Support/AddressBook`).
Photos are published separately as raw bytes to HA MQTT `image` entities
(`<host>/images/<slug>`) rather than inlined as base64 into every event
payload, which previously bloated state-mirror messages past 100 KB.

Outbound sending (`outbound.py`) is the one inbound path on the comms side:
it subscribes to `<prefix>/<host>/comms/messages/send`, drives Messages.app
through `osascript`, and publishes the result to `.../send_result`.

## The phase-ticker pattern

Every phase implements `phases/base.AbstractTicker`: `enabled`,
`interval_seconds` (or `None` for a self-paced loop), and `run_once(mqtt)`.
`runtime.Bridge` instantiates one `asyncio` task per enabled ticker at
startup, each looping `run_once()` → sleep `interval_seconds` independently —
so a ticker with a 3600-second cadence (`software_updates`, which shells out
to `softwareupdate -l --no-scan`) never blocks one with a 3-second cadence
(`focused_app`).

Adding a phase means adding one `AbstractTicker` subclass and enabling it in
config; the scheme is why this project has 21 of them rather than one
monolithic "collect everything" loop; see [Configuration](README.md#configuration)
for the exact list with defaults, or `config.example.yaml` for every option
inline.

## Configuration and identity

`config.py`'s `pydantic` models (`extra="forbid"`, so a typo in `config.yaml`
fails loudly instead of silently doing nothing) load `config.yaml` through
`ha_mqtt_bridge.load_yaml_with_env`, which expands `${ENV_VAR}` references —
used for `${DARWIN_USER_DIR}` in the two Screen Time store paths and for MQTT
credentials, which are never written into the YAML file itself.

At startup, `cli.py` resolves this Mac's identity — hostname slug, "friendly"
name (`scutil --get ComputerName`), hardware serial (`ioreg`), and the `en0`
MAC address (`networksetup`) — and passes all four into `MqttPublisher`,
which builds one Home Assistant device block
(`identifiers: ["macos-mqtt-bridge:<slug>", "apple-serial:<serial>"]`,
`connections: [["mac", "<mac>"]]`) that every sensor from every phase and
every comms source shares. That's the single mechanism that makes "every
sensor this bridge publishes shows up under one device in Home Assistant"
true without each phase needing to know about identity resolution itself.

## Layout

| Path | Role |
|---|---|
| `src/macos_bridge/runtime.py` | `Bridge`: owns the MQTT connection, starts the comms thread + all phase-ticker asyncio tasks, wires signal handling. |
| `src/macos_bridge/sources/` | Comms event sources: `messages.py`, `calls.py`, `voicemail.py`. |
| `src/macos_bridge/phases/` | The 21 phase tickers, one file each, all implementing `base.AbstractTicker`. |
| `src/macos_bridge/contacts.py` | AddressBook indexing + phone/email → name/photo resolution. |
| `src/macos_bridge/mqtt.py` | `MqttPublisher` — macOS-specific identity + HA device block on top of the shared `ThreadedPublisher`. |
| `src/macos_bridge/discovery.py` | Static + derived HA discovery entity definitions for the comms half. |
| `src/macos_bridge/outbound.py` | Inbound `messages/send` → `osascript` dispatch. |
| `src/macos_bridge/realtime_calls.py` | Reads JSON lines from the Swift `CallObserver` subprocess. |
| `src/macos_bridge/controls.py` | Inbound HA controls (volume, lock screen, speak text, …) → shell/AppleScript. |
| `src/macos_bridge/config.py` | The `pydantic` config schema + `${ENV_VAR}` loading. |
| `src/macos_bridge/state.py` | The `state.json` high-water-mark store (per-source last-seen IDs). |
| `src/macos_bridge/cli.py` | `macos-mqtt-bridge` entry point: `run` / `init-state` / `dump-once` / `bootstrap-allowlist`. |
| `helpers/call-observer/` | Swift/Mac-Catalyst `CXCallObserver` subprocess (real-time call state). |
| `helpers/location-fetcher/` | Swift `CoreLocation` helper (one-shot fix, ad-hoc signed so the Location Services grant survives venv rebuilds). |
| `install.sh` | Idempotent installer: venv via `uv`, build both Swift helpers, install the `launchd` LaunchAgent. |

## Dependencies

- `_shared/ha-mqtt-bridge-toolkit/` (vendored into this repository at
  publish time) — `ThreadedPublisher`, the disk-backed `Outbox`, HA
  discovery payload builders, topic slugging, and `${ENV_VAR}`-aware YAML
  config loading, shared with this project's sibling bridges. See
  [Credits](README.md#credits).
- `_shared/python-github-error-reporter/` (vendored into this repository at
  publish time) — reports unhandled exceptions via GitHub
  `repository_dispatch`; entirely inert unless `GITHUB_ERROR_TOKEN`/
  `GITHUB_REPO` are set (see [Permissions](README.md#permissions)).
- `paho-mqtt` — the MQTT client underneath `ThreadedPublisher`.
- `pydantic` — the config schema.
- `pyyaml` — `config.yaml` parsing.
