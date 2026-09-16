"""Calls source tests against real CallHistory.storedata fixture."""

from __future__ import annotations

import sqlite3
from collections import Counter

from macos_bridge.config import CallsSource as CallsConfig
from macos_bridge.sources.calls import CallsSource
from macos_bridge.state import State


def _max_pk(db_path) -> int:
    with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as conn:
        return int(conn.execute("SELECT MAX(Z_PK) FROM ZCALLRECORD").fetchone()[0] or 0)


def test_init_state_primes(calls_db, state_path):
    source = CallsSource(CallsConfig(), str(calls_db))
    state = State(str(state_path))
    source.init_state(state)
    assert state.get("calls_last_pk") == _max_pk(calls_db)
    assert state.get("calls_primed") is True


def test_first_poll_without_prime_skips_backlog(calls_db, state_path):
    source = CallsSource(CallsConfig(), str(calls_db))
    state = State(str(state_path))
    events = list(source.poll(state))
    assert events == []
    assert state.get("calls_primed") is True


def test_poll_emits_started_and_ended_for_answered_call(calls_db, state_path):
    """Find a real answered call (incoming, ZANSWERED=1), set last_pk just
    before it, and verify the source emits both started and ended events."""
    with sqlite3.connect(f"file:{calls_db}?mode=ro", uri=True) as conn:
        row = conn.execute(
            "SELECT Z_PK FROM ZCALLRECORD WHERE ZANSWERED = 1 AND ZORIGINATED = 0 "
            "ORDER BY Z_PK DESC LIMIT 1"
        ).fetchone()
    assert row, "fixture has no answered incoming calls"
    pk = int(row[0])

    state = State(str(state_path))
    state.set("calls_last_pk", pk - 1)
    state.set("calls_primed", True)
    state.save()

    source = CallsSource(CallsConfig(), str(calls_db))
    # Restrict to that single row by setting last_pk just below it; subsequent
    # calls may exist but they all just get emitted along with it. We filter.
    events = [e for e in source.poll(state) if e[1].get("pk") == pk]
    paths = [e[0] for e in events]
    assert any(p.endswith("/started") for p in paths)
    assert any(p.endswith("/ended") for p in paths)
    for _, payload in events:
        assert payload["direction"] == "incoming"
        assert payload["answered"] is True
        assert payload["service"] in {"com.apple.Telephony", "com.apple.FaceTime"}


def test_poll_emits_missed_for_unanswered_incoming(calls_db, state_path):
    with sqlite3.connect(f"file:{calls_db}?mode=ro", uri=True) as conn:
        row = conn.execute(
            "SELECT Z_PK FROM ZCALLRECORD WHERE ZANSWERED = 0 AND ZORIGINATED = 0 "
            "ORDER BY Z_PK DESC LIMIT 1"
        ).fetchone()
    assert row, "fixture has no missed incoming calls"
    pk = int(row[0])

    state = State(str(state_path))
    state.set("calls_last_pk", pk - 1)
    state.set("calls_primed", True)
    state.save()

    source = CallsSource(CallsConfig(), str(calls_db))
    events = [e for e in source.poll(state) if e[1].get("pk") == pk]
    paths = [e[0] for e in events]
    # missed events should be emitted; no started/ended for missed.
    assert any(p.endswith("/missed") for p in paths)
    assert not any(p.endswith("/started") for p in paths)
    assert not any(p.endswith("/ended") for p in paths)
    for _, payload in events:
        assert payload["direction"] == "incoming"
        assert payload["answered"] is False


def test_phone_vs_facetime_classification(calls_db, state_path):
    state = State(str(state_path))
    state.set("calls_last_pk", 0)
    state.set("calls_primed", True)
    state.save()

    source = CallsSource(CallsConfig(), str(calls_db))
    events = list(source.poll(state))
    namespaces = Counter(e[0].split("/")[0] for e in events)

    with sqlite3.connect(f"file:{calls_db}?mode=ro", uri=True) as conn:
        truth_phone = conn.execute(
            "SELECT COUNT(*) FROM ZCALLRECORD WHERE LOWER(ZSERVICE_PROVIDER) LIKE '%telephony%'"
        ).fetchone()[0]
        truth_facetime = conn.execute(
            "SELECT COUNT(*) FROM ZCALLRECORD WHERE LOWER(ZSERVICE_PROVIDER) LIKE '%facetime%'"
        ).fetchone()[0]

    # Each phone/facetime call produces 1 (missed) or 2 (started+ended) events.
    # So namespace counts are between truth_x and 2*truth_x.
    assert truth_phone <= namespaces.get("phone", 0) <= 2 * truth_phone
    assert truth_facetime <= namespaces.get("facetime", 0) <= 2 * truth_facetime


def test_event_payload_has_required_fields(calls_db, state_path):
    max_pk = _max_pk(calls_db)
    state = State(str(state_path))
    state.set("calls_last_pk", max_pk - 5)
    state.set("calls_primed", True)
    state.save()

    source = CallsSource(CallsConfig(), str(calls_db))
    events = list(source.poll(state))
    assert events

    required = {
        "pk", "unique_id", "service", "address", "direction", "answered",
        "call_type", "duration_seconds", "started_at", "ended_at",
    }
    for event_path, payload in events:
        missing = required - payload.keys()
        assert not missing, f"event {event_path} missing keys: {missing}"
        assert payload["direction"] in {"incoming", "outgoing"}
        assert isinstance(payload["answered"], bool)


def test_contact_enrichment_when_resolver_provided(calls_db, state_path, contact_resolver):
    state = State(str(state_path))
    state.set("calls_last_pk", 0)
    state.set("calls_primed", True)
    state.save()

    source = CallsSource(CallsConfig(), str(calls_db), contact_resolver)
    events = list(source.poll(state))
    enriched = [p for _, p in events if "contact" in p]
    assert enriched, "no call events were enriched with contact info"

    sample = enriched[0]["contact"]
    assert "uid" in sample
    assert "full_name" in sample


def test_emit_started_event_can_be_disabled(calls_db, state_path):
    max_pk = _max_pk(calls_db)
    state = State(str(state_path))
    state.set("calls_last_pk", max_pk - 10)
    state.set("calls_primed", True)
    state.save()

    cfg = CallsConfig(emit_started_event=False)
    source = CallsSource(cfg, str(calls_db))
    events = list(source.poll(state))
    assert events
    paths = [e[0] for e in events]
    assert not any(p.endswith("/started") for p in paths)
    # ended and missed are still produced
    assert any(p.endswith("/ended") or p.endswith("/missed") for p in paths)
