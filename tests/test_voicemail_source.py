"""Voicemail source tests against the real FaceTimeMessageStore-local.sqlitedb fixture."""

from __future__ import annotations

import sqlite3
from collections import Counter
from pathlib import Path

import pytest

from macos_bridge.config import VoicemailSource as VoicemailConfig
from macos_bridge.sources.voicemail import (
    VoicemailSource,
    _decode_transcript,
    _format_uuid,
)
from macos_bridge.state import State


def _max_pk(db_path: Path) -> int:
    with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as conn:
        return int(conn.execute("SELECT MAX(Z_PK) FROM ZSTOREDMESSAGE").fetchone()[0] or 0)


def test_format_uuid_round_trip():
    raw = bytes.fromhex("4D94EC8088FD57E3B37EF98823AB849C")
    assert _format_uuid(raw) == "4D94EC80-88FD-57E3-B37E-F98823AB849C"
    assert _format_uuid(None) is None
    assert _format_uuid(b"") is None
    assert _format_uuid(b"too short") is None


def test_decode_transcript_against_real_blob(voicemail_db, tmp_path):
    """Pull a real ZTRANSCRIPT blob and verify the bplist NSKeyedArchiver
    decoder extracts the transcription string."""
    with sqlite3.connect(f"file:{voicemail_db}?mode=ro", uri=True) as conn:
        rows = conn.execute(
            "SELECT ZTRANSCRIPT FROM ZSTOREDMESSAGE "
            "WHERE ZTRANSCRIPT IS NOT NULL ORDER BY Z_PK DESC LIMIT 20"
        ).fetchall()
    assert rows, "no transcripts in fixture"

    decoded = [_decode_transcript(r[0]) for r in rows]
    non_empty = [s for s in decoded if s and len(s) >= 3]
    # At least 80% of recent transcripts should produce non-trivial text.
    assert len(non_empty) >= 0.8 * len(decoded), (
        f"only {len(non_empty)}/{len(decoded)} transcripts decoded with text"
    )


def test_init_state_primes(voicemail_db, state_path):
    cfg = VoicemailConfig()
    source = VoicemailSource(cfg, str(voicemail_db), None)
    state = State(str(state_path))
    source.init_state(state)
    assert state.get("voicemail_last_pk") == _max_pk(voicemail_db)
    assert state.get("voicemail_primed") is True


def test_first_poll_without_prime_skips_backlog(voicemail_db, state_path):
    cfg = VoicemailConfig()
    source = VoicemailSource(cfg, str(voicemail_db), None)
    state = State(str(state_path))
    events = list(source.poll(state))
    assert events == []
    assert state.get("voicemail_primed") is True


def test_phone_voicemails_route_to_voicemail_topic(voicemail_db, state_path):
    """Set last_pk to 0 with primed=True so all rows look 'new'. Verify phone
    voicemails route to voicemail/received."""
    cfg = VoicemailConfig()
    state = State(str(state_path))
    state.set("voicemail_last_pk", 0)
    state.set("voicemail_primed", True)
    state.save()

    source = VoicemailSource(cfg, str(voicemail_db), None)
    events = list(source.poll(state))
    topics = Counter(e[0] for e in events)
    assert topics["voicemail/received"] > 0


def test_facetime_messages_route_to_facetime_topic(voicemail_db, state_path):
    cfg = VoicemailConfig()
    state = State(str(state_path))
    state.set("voicemail_last_pk", 0)
    state.set("voicemail_primed", True)
    state.save()

    source = VoicemailSource(cfg, str(voicemail_db), None)
    events = list(source.poll(state))
    topics = Counter(e[0] for e in events)
    # The fixture has at least one FaceTime audio message
    assert topics.get("facetime/audio_message_received", 0) >= 1


def test_deleted_messages_excluded_by_default(voicemail_db, state_path):
    """ZMAILBOXTYPE=2 rows should be skipped unless include_deleted=True."""
    cfg = VoicemailConfig()
    state = State(str(state_path))
    state.set("voicemail_last_pk", 0)
    state.set("voicemail_primed", True)
    state.save()

    source = VoicemailSource(cfg, str(voicemail_db), None)
    events = list(source.poll(state))
    for _, payload in events:
        assert payload["mailbox_type"] != 2

    with sqlite3.connect(f"file:{voicemail_db}?mode=ro", uri=True) as conn:
        truth_inbox = conn.execute(
            "SELECT COUNT(*) FROM ZSTOREDMESSAGE WHERE ZMAILBOXTYPE != 2"
        ).fetchone()[0]
    # Allow some slack: rows with unrecognized providers are skipped.
    assert len(events) <= truth_inbox


def test_include_deleted_flag(voicemail_db, state_path):
    cfg = VoicemailConfig(include_deleted=True)
    state = State(str(state_path))
    state.set("voicemail_last_pk", 0)
    state.set("voicemail_primed", True)
    state.save()

    source = VoicemailSource(cfg, str(voicemail_db), None)
    events = list(source.poll(state))
    deleted = sum(1 for _, p in events if p["mailbox_type"] == 2)
    assert deleted > 0


def test_event_payload_required_fields(voicemail_db, state_path):
    cfg = VoicemailConfig()
    state = State(str(state_path))
    max_pk = _max_pk(voicemail_db)
    state.set("voicemail_last_pk", max_pk - 20)
    state.set("voicemail_primed", True)
    state.save()

    source = VoicemailSource(cfg, str(voicemail_db), None)
    events = list(source.poll(state))
    assert events
    required = {
        "pk", "uuid", "provider", "kind", "sender", "duration_seconds",
        "timestamp", "transcription_status", "is_read",
    }
    for _, payload in events:
        missing = required - payload.keys()
        assert not missing, f"missing keys: {missing}"
        assert payload["kind"] in {"phone", "facetime"}


def test_audio_path_resolution_from_real_assets(
    voicemail_db, voicemail_assets_dir, state_path
):
    """When assets_dir is provided and a row's ZRECORDUUID matches an audio
    file on disk, the payload should include the resolved audio_path."""
    if voicemail_assets_dir is None:
        pytest.skip("Assets dir fixture not present")

    cfg = VoicemailConfig()
    state = State(str(state_path))
    state.set("voicemail_last_pk", 0)
    state.set("voicemail_primed", True)
    state.save()

    source = VoicemailSource(cfg, str(voicemail_db), str(voicemail_assets_dir))
    events = list(source.poll(state))

    with_path = [p for _, p in events if p.get("audio_path")]
    assert with_path, "no events resolved an audio file path"

    # Verify resolution: the audio_path file should exist and contain the UUID
    sample = with_path[0]
    assert Path(sample["audio_path"]).exists()
    assert sample["uuid"] in sample["audio_path"]


def test_contact_enrichment_when_resolver_provided(voicemail_db, state_path, contact_resolver):
    cfg = VoicemailConfig()
    state = State(str(state_path))
    state.set("voicemail_last_pk", 0)
    state.set("voicemail_primed", True)
    state.save()

    source = VoicemailSource(cfg, str(voicemail_db), None, contact_resolver)
    events = list(source.poll(state))
    enriched = [p for _, p in events if "contact" in p]
    assert enriched, "no voicemail events were enriched with contact info"

    sample = enriched[0]["contact"]
    assert "uid" in sample
    assert "full_name" in sample


def test_include_transcription_true_by_default(voicemail_db, state_path):
    cfg = VoicemailConfig()
    state = State(str(state_path))
    state.set("voicemail_last_pk", 0)
    state.set("voicemail_primed", True)
    state.save()

    source = VoicemailSource(cfg, str(voicemail_db), None)
    events = list(source.poll(state))
    assert events
    assert any("transcription" in payload for _, payload in events)


def test_include_transcription_false_omits_transcript(voicemail_db, state_path):
    """Mirrors sources.messages.include_text: the key is omitted (not null)
    when disabled, for both event kinds this source emits."""
    cfg = VoicemailConfig(include_transcription=False)
    state = State(str(state_path))
    state.set("voicemail_last_pk", 0)
    state.set("voicemail_primed", True)
    state.save()

    source = VoicemailSource(cfg, str(voicemail_db), None)
    events = list(source.poll(state))
    assert events
    topics = {path for path, _ in events}
    assert "voicemail/received" in topics
    assert "facetime/audio_message_received" in topics
    for _, payload in events:
        assert "transcription" not in payload


def test_include_audio_path_false(voicemail_db, voicemail_assets_dir, state_path):
    if voicemail_assets_dir is None:
        pytest.skip("Assets dir fixture not present")

    cfg = VoicemailConfig(include_audio_path=False)
    state = State(str(state_path))
    state.set("voicemail_last_pk", 0)
    state.set("voicemail_primed", True)
    state.save()

    source = VoicemailSource(cfg, str(voicemail_db), str(voicemail_assets_dir))
    events = list(source.poll(state))
    assert events
    for _, payload in events:
        assert "audio_path" not in payload
