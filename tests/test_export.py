"""export-events: the last N days of comms events in the live payload shape, read-only."""
from __future__ import annotations

import json
import sqlite3

from macos_bridge.config import CallsSource as CallsConfig
from macos_bridge.config import MessagesSource as MessagesConfig
from macos_bridge.config import VoicemailSource as VoicemailConfig
from macos_bridge.export import export_events
from macos_bridge.sources.calls import CallsSource
from macos_bridge.sources.messages import MessagesSource
from macos_bridge.sources.voicemail import VoicemailSource
from macos_bridge.time_utils import mac_absolute_to_unix


def _median_unix(db, sql):
    with sqlite3.connect(f"file:{db}?mode=ro", uri=True) as conn:
        values = sorted(r[0] for r in conn.execute(sql) if r[0])
    return mac_absolute_to_unix(values[len(values) // 2])


def test_only_events_newer_than_the_cutoff_are_exported_in_the_live_shape(messages_db):
    source = MessagesSource(MessagesConfig(), str(messages_db))
    cutoff = _median_unix(messages_db, "SELECT date FROM message")
    lines = []
    n = export_events([source], cutoff, "macos", "testmac", lines.append)
    assert n == len(lines) > 0
    for line in lines:
        record = json.loads(line)
        assert record["topic"].startswith("macos/testmac/messages/")
        payload = record["payload"]
        assert payload["host"] == "testmac" and not any(k.startswith("__") for k in payload)
        assert payload["timestamp"] >= __import__("datetime").datetime.fromtimestamp(
            cutoff, __import__("datetime").timezone.utc).isoformat()[:19]


def test_calls_and_voicemail_export_too(calls_db, voicemail_db):
    calls = CallsSource(CallsConfig(), str(calls_db))
    voicemail = VoicemailSource(VoicemailConfig(), str(voicemail_db), None)
    lines = []
    export_events([calls, voicemail], 0.0, "macos", "testmac", lines.append)
    paths = {json.loads(line)["topic"].split("/", 2)[2].split("/")[0] for line in lines}
    assert {"voicemail"} <= paths and paths & {"phone", "facetime"}


def test_a_cutoff_in_the_future_exports_nothing(messages_db):
    source = MessagesSource(MessagesConfig(), str(messages_db))
    assert export_events([source], 4_102_444_800.0, "macos", "testmac", [].append) == 0


def test_id_before_uses_each_sources_own_dates(messages_db, calls_db):
    assert MessagesSource(MessagesConfig(), str(messages_db)).id_before(0.0) == 0
    assert CallsSource(CallsConfig(), str(calls_db)).id_before(4_102_444_800.0) > 0
