"""macos-mqtt-bridge — unified macOS → MQTT bridge.

Combines two formerly-separate daemons (screen-time-ha-bridge and
macos-comms-mqtt-bridge) into one process that:

  - Polls SQLite event sources (Messages, Phone, FaceTime, Voicemail) and
    emits per-event MQTT messages with persistent rowid state and a
    disk-backed outbox for broker-down resilience.
  - Spawns a Swift CXCallObserver subprocess for real-time call state.
  - Subscribes to an outbound topic and sends iMessage/SMS via osascript.
  - Runs seven Phase tickers (knowledgeC.db aggregates, per-app, focused
    app, family devices, activity, system state, host metrics) and emits
    retained state topics with HA MQTT discovery.

All sources and phases share a single MQTT connection.
"""

__version__ = "0.1.0"
