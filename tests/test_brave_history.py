"""Tests for the Brave Browser per-visit browsing-events ticker.

Builds a synthetic Chromium ``History`` SQLite DB (the subset of the
``urls`` / ``visits`` schema the ticker's query touches) under a fake
Brave Application Support tree, then runs the ticker and asserts the
published per-visit JSON payload.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

import pytest

from macos_bridge.phases.brave_history import (
    CHROME_EPOCH_UNIX_OFFSET_SECONDS,
    BraveHistoryTicker,
)

# Fixed anchor so the "today" boundary is deterministic regardless of when
# the suite runs (avoids midnight-boundary flakiness).
ANCHOR = datetime(2026, 5, 28, 15, 0, 0, tzinfo=ZoneInfo("UTC"))


def _chrome_us(dt: datetime) -> int:
    """Python datetime → Chrome-epoch microseconds (since 1601-01-01 UTC)."""
    return int((dt.timestamp() + CHROME_EPOCH_UNIX_OFFSET_SECONDS) * 1_000_000)


def _build_history_db(path: Path, rows: list[tuple[int, str, str]],
                      visits: list[tuple[int, datetime, int]]) -> None:
    """Write a minimal Chromium History DB.

    ``rows``   = (url_id, url, title)
    ``visits`` = (url_id, visit_dt, visit_duration_microseconds)
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            "CREATE TABLE urls (id INTEGER PRIMARY KEY, url TEXT, title TEXT)"
        )
        conn.execute(
            "CREATE TABLE visits (id INTEGER PRIMARY KEY, url INTEGER, "
            "visit_time INTEGER, visit_duration INTEGER)"
        )
        conn.executemany("INSERT INTO urls (id, url, title) VALUES (?,?,?)", rows)
        conn.executemany(
            "INSERT INTO visits (url, visit_time, visit_duration) VALUES (?,?,?)",
            [(uid, _chrome_us(dt), dur) for uid, dt, dur in visits],
        )
        conn.commit()
    finally:
        conn.close()


@pytest.fixture
def fake_mqtt() -> MagicMock:
    m = MagicMock()
    m.host_slug = "test_host"
    return m


@pytest.fixture(autouse=True)
def _freeze_now(monkeypatch: pytest.MonkeyPatch):
    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return ANCHOR.astimezone(tz)

    monkeypatch.setattr("macos_bridge.phases.brave_history.datetime", FrozenDatetime)


def _ticker(brave_home: Path, tmp_path: Path, **kw) -> BraveHistoryTicker:
    return BraveHistoryTicker(
        enabled=True,
        interval_seconds=900,
        brave_home=brave_home,
        tmp_dir=tmp_path / "work",
        host_slug="test_host",
        topic_prefix="macos",
        availability_topic="macos/test_host/status",
        local_timezone=ZoneInfo("UTC"),
        **kw,
    )


def test_emits_one_event_per_visit_with_start_and_duration(tmp_path: Path, fake_mqtt: MagicMock):
    brave_home = tmp_path / "Brave-Browser"
    history = brave_home / "Default" / "History"
    v1 = ANCHOR - timedelta(hours=2)   # today
    v2 = ANCHOR - timedelta(hours=1)   # today, same url, distinct visit
    _build_history_db(
        history,
        rows=[
            (1, "https://foo.com/a", "Foo A"),
            (2, "https://bar.com/b", "Bar B"),
            (3, "https://old.com/c", "Old C"),
            (4, "https://zero.com/d", "Zero D"),
        ],
        visits=[
            (1, v1, 120_000_000),                       # 120 s
            (1, v2, 60_000_000),                        # 60 s — SAME url, separate visit
            (2, ANCHOR - timedelta(minutes=30), 30_000_000),  # 30 s
            (3, ANCHOR - timedelta(days=1), 99_000_000),      # yesterday → excluded
            (4, ANCHOR - timedelta(minutes=5), 0),            # zero duration → excluded
        ],
    )

    ticker = _ticker(brave_home, tmp_path)
    asyncio.run(ticker.run_once(fake_mqtt))

    assert fake_mqtt.publish_state.call_count == 1
    topic, raw = fake_mqtt.publish_state.call_args.args
    assert topic == "macos/test_host/today/web/Default"

    payload = json.loads(raw)
    assert payload["date"] == "2026-05-28"
    assert payload["profile"] == "Default"
    # Three of today's visits survive (old.com yesterday + zero.com 0-dur dropped).
    # Crucially BOTH foo.com visits appear as separate events (not summed).
    assert payload["visit_count"] == 3
    assert len(payload["visits"]) == 3

    foo = [v for v in payload["visits"] if v["url"] == "https://foo.com/a"]
    assert len(foo) == 2
    assert {round(v["duration_s"]) for v in foo} == {120, 60}
    assert all(v["title"] == "Foo A" for v in foo)
    # Newest first; each carries an ISO-8601 start with offset.
    starts = [v["visit_start"] for v in payload["visits"]]
    assert starts == sorted(starts, reverse=True)
    assert "T" in starts[0] and ("+" in starts[0] or "Z" in starts[0])


def test_multiple_profiles_each_publish(tmp_path: Path, fake_mqtt: MagicMock):
    brave_home = tmp_path / "Brave-Browser"
    _build_history_db(
        brave_home / "Default" / "History",
        rows=[(1, "https://foo.com", "Foo")],
        visits=[(1, ANCHOR - timedelta(hours=1), 10_000_000)],
    )
    _build_history_db(
        brave_home / "Profile 1" / "History",
        rows=[(1, "https://bar.com", "Bar")],
        visits=[(1, ANCHOR - timedelta(hours=1), 20_000_000)],
    )

    ticker = _ticker(brave_home, tmp_path)
    asyncio.run(ticker.run_once(fake_mqtt))

    topics = {c.args[0] for c in fake_mqtt.publish_state.call_args_list}
    assert topics == {
        "macos/test_host/today/web/Default",
        "macos/test_host/today/web/Profile_1",
    }


def test_max_visits_per_profile_caps_output(tmp_path: Path, fake_mqtt: MagicMock):
    brave_home = tmp_path / "Brave-Browser"
    history = brave_home / "Default" / "History"
    _build_history_db(
        history,
        rows=[(1, "https://site.com", "Site")],
        visits=[
            (1, ANCHOR - timedelta(minutes=i), 5_000_000) for i in range(1, 11)
        ],
    )

    ticker = _ticker(brave_home, tmp_path, max_visits_per_profile=3)
    asyncio.run(ticker.run_once(fake_mqtt))

    payload = json.loads(fake_mqtt.publish_state.call_args.args[1])
    assert payload["visit_count"] == 3  # newest 3 visits only


def test_missing_brave_home_is_a_no_op(tmp_path: Path, fake_mqtt: MagicMock):
    ticker = _ticker(tmp_path / "does-not-exist", tmp_path)
    asyncio.run(ticker.run_once(fake_mqtt))
    assert fake_mqtt.publish_state.call_count == 0
