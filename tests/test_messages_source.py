"""Messages source tests against real chat.db fixture."""

from __future__ import annotations

import sqlite3

import pytest

from macos_bridge.config import MessagesSource as MessagesConfig
from macos_bridge.sources.messages import MessagesSource, _extract_attributed_text
from macos_bridge.state import State


def _max_rowid(db_path) -> int:
    with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as conn:
        return int(conn.execute("SELECT MAX(ROWID) FROM message").fetchone()[0] or 0)


def test_init_state_records_current_max_and_primes(messages_db, state_path):
    source = MessagesSource(MessagesConfig(), str(messages_db))
    state = State(str(state_path))
    source.init_state(state)
    assert state.get("messages_last_rowid") == _max_rowid(messages_db)
    assert state.get("messages_primed") is True


def test_first_poll_without_init_primes_silently(messages_db, state_path):
    source = MessagesSource(MessagesConfig(), str(messages_db))
    state = State(str(state_path))  # primed=False
    events = list(source.poll(state))
    assert events == []
    assert state.get("messages_primed") is True
    assert state.get("messages_last_rowid") == _max_rowid(messages_db)


def test_poll_after_prime_yields_only_new_rows(messages_db, state_path):
    """Set last-seen to N-5, expect 5 new events (one per row added since)."""
    max_rowid = _max_rowid(messages_db)
    state = State(str(state_path))
    state.set("messages_last_rowid", max_rowid - 5)
    state.set("messages_primed", True)
    state.save()

    source = MessagesSource(MessagesConfig(), str(messages_db))
    events = list(source.poll(state))
    assert len(events) == 5
    assert state.get("messages_last_rowid") == max_rowid

    for event_path, payload in events:
        assert event_path in {"messages/sent", "messages/received"}
        assert "rowid" in payload
        assert "guid" in payload
        assert "service" in payload
        assert payload["service"] in {"iMessage", "SMS", "RCS", None}
        assert "timestamp" in payload
        assert "chat" in payload
        assert "is_group" in payload["chat"]


def test_text_present_or_attributed_body_decoded(messages_db, state_path):
    """Real corpus shows ~99% of recent messages use attributedBody, not text.
    The decoder should produce a non-None text for the majority of those."""
    max_rowid = _max_rowid(messages_db)
    state = State(str(state_path))
    state.set("messages_last_rowid", max_rowid - 200)
    state.set("messages_primed", True)
    state.save()

    source = MessagesSource(MessagesConfig(), str(messages_db))
    events = list(source.poll(state))
    assert len(events) > 0

    decoded = sum(1 for _, p in events if p.get("text"))
    # At least half of recent messages should have decodable body text.
    # The remainder are typically reactions/stickers/attachment-only.
    assert decoded >= len(events) // 2, (
        f"only {decoded}/{len(events)} events had text — decoder may be broken"
    )


def test_include_text_false_omits_body(messages_db, state_path):
    max_rowid = _max_rowid(messages_db)
    state = State(str(state_path))
    state.set("messages_last_rowid", max_rowid - 3)
    state.set("messages_primed", True)
    state.save()

    cfg = MessagesConfig(include_text=False)
    source = MessagesSource(cfg, str(messages_db))
    events = list(source.poll(state))
    assert events
    for _, payload in events:
        assert "text" not in payload


def test_attributed_body_decoder_matches_text_column(messages_db, state_path):
    """When a message has both `text` and `attributedBody`, decoder output
    should match the text column verbatim."""
    with sqlite3.connect(f"file:{messages_db}?mode=ro", uri=True) as conn:
        rows = conn.execute(
            "SELECT text, attributedBody FROM message "
            "WHERE text IS NOT NULL AND attributedBody IS NOT NULL "
            "ORDER BY ROWID DESC LIMIT 20"
        ).fetchall()
    assert rows, "no messages with both text and attributedBody in fixture"

    matches = 0
    for text, blob in rows:
        decoded = _extract_attributed_text(blob)
        if decoded == text:
            matches += 1
    assert matches >= len(rows) // 2, (
        f"decoder matched text column in only {matches}/{len(rows)} cases"
    )


def test_contact_enrichment_when_resolver_provided(messages_db, state_path, contact_resolver):
    """When a ContactResolver is passed, recent message events should have a
    populated `contact` sub-object for handles that match the AddressBook."""
    max_rowid = _max_rowid(messages_db)
    state = State(str(state_path))
    state.set("messages_last_rowid", max_rowid - 200)
    state.set("messages_primed", True)
    state.save()

    source = MessagesSource(MessagesConfig(), str(messages_db), contact_resolver)
    events = list(source.poll(state))
    enriched = [p for _, p in events if "contact" in p]
    assert enriched, "no events were enriched with contact info"

    # Spot-check shape of the contact sub-object.
    sample = enriched[0]["contact"]
    assert "uid" in sample
    assert "full_name" in sample
    # photo bytes are NOT in the JSON payload — they are stashed under
    # the internal `__photo_bytes` key on the event dict and stripped by
    # runtime._emit before serialization (then republished separately to
    # the entity's HA MQTT image topic).
    assert "photo_bytes" not in sample
    assert "photo_data_uri" not in sample


def test_send_received_classification(messages_db, state_path):
    """is_from_me must drive sent vs received for normal-text messages."""
    max_rowid = _max_rowid(messages_db)
    state = State(str(state_path))
    state.set("messages_last_rowid", max_rowid - 50)
    state.set("messages_primed", True)
    state.save()

    source = MessagesSource(MessagesConfig(), str(messages_db))
    events = list(source.poll(state))

    with sqlite3.connect(f"file:{messages_db}?mode=ro", uri=True) as conn:
        rows = conn.execute(
            "SELECT ROWID, is_from_me, item_type, associated_message_type "
            "FROM message WHERE ROWID > ?",
            (max_rowid - 50,),
        ).fetchall()
    # Only normal messages should be on sent/received topics.
    normal = {
        r[0]: bool(r[1])
        for r in rows
        if r[2] == 0 and not (2000 <= (r[3] or 0) < 3100)
    }

    for event_path, payload in events:
        if event_path in ("messages/sent", "messages/received"):
            assert payload["rowid"] in normal
            expected = "messages/sent" if normal[payload["rowid"]] else "messages/received"
            assert event_path == expected


def test_reactions_route_to_reaction_topic(messages_db, state_path):
    """Find a real reaction row and verify it routes to messages/reaction."""
    with sqlite3.connect(f"file:{messages_db}?mode=ro", uri=True) as conn:
        row = conn.execute(
            "SELECT ROWID, associated_message_type FROM message "
            "WHERE associated_message_type BETWEEN 2000 AND 2010 "
            "ORDER BY ROWID DESC LIMIT 1"
        ).fetchone()
    assert row, "no reactions in fixture"
    target_rowid = row[0]

    state = State(str(state_path))
    state.set("messages_last_rowid", target_rowid - 1)
    state.set("messages_primed", True)
    state.save()

    source = MessagesSource(MessagesConfig(), str(messages_db))
    events = list(source.poll(state))
    matching = [e for e in events if e[1].get("rowid") == target_rowid]
    assert matching
    event_path, payload = matching[0]
    assert event_path == "messages/reaction"
    assert payload["reaction_kind"] in {
        "loved", "liked", "disliked", "laughed", "emphasized", "questioned", "sticker"
    }
    assert payload["is_remove"] is False
    assert payload["target_guid"] is not None
    # Reaction target_guid should not have the p:N/ prefix.
    assert not payload["target_guid"].startswith("p:")


def test_group_events_route_to_group_topic(messages_db, state_path):
    """Group participant change → messages/group_event."""
    with sqlite3.connect(f"file:{messages_db}?mode=ro", uri=True) as conn:
        row = conn.execute(
            "SELECT ROWID FROM message WHERE item_type != 0 ORDER BY ROWID DESC LIMIT 1"
        ).fetchone()
    assert row, "no group events in fixture"
    target_rowid = row[0]

    state = State(str(state_path))
    state.set("messages_last_rowid", target_rowid - 1)
    state.set("messages_primed", True)
    state.save()

    source = MessagesSource(MessagesConfig(), str(messages_db))
    events = list(source.poll(state))
    matching = [e for e in events if e[1].get("rowid") == target_rowid]
    assert matching
    event_path, payload = matching[0]
    assert event_path == "messages/group_event"
    assert "item_type" in payload
    assert "group_action_type" in payload


def test_edited_messages_route_to_edited_topic(messages_db, state_path):
    """date_edited != 0 → messages/edited."""
    with sqlite3.connect(f"file:{messages_db}?mode=ro", uri=True) as conn:
        row = conn.execute(
            "SELECT ROWID FROM message WHERE date_edited != 0 ORDER BY ROWID DESC LIMIT 1"
        ).fetchone()
    if not row:
        pytest.skip("no edited messages in fixture")
    target_rowid = row[0]

    state = State(str(state_path))
    state.set("messages_last_rowid", target_rowid - 1)
    state.set("messages_primed", True)
    state.save()

    source = MessagesSource(MessagesConfig(), str(messages_db))
    events = list(source.poll(state))
    matching = [e for e in events if e[1].get("rowid") == target_rowid]
    assert matching
    event_path, payload = matching[0]
    assert event_path == "messages/edited"
    assert payload["edited_at"] is not None
