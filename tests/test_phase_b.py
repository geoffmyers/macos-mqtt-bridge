"""Tests for Phase B per-app per-day usage ticker."""

from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

import pytest

from macos_bridge.config import AppEntry
from macos_bridge.phases.per_app import PerAppTicker


@pytest.fixture
def fake_mqtt() -> MagicMock:
    m = MagicMock()
    m.host_slug = "test_host"
    m.sw_version = "0.1.0"
    m.host_friendly_name = "Test Host"
    return m


@pytest.fixture(autouse=True)
def _stub_bundle_lookup(monkeypatch: pytest.MonkeyPatch):
    """Stub bundle_id_to_app_name to avoid invoking mdfind in tests."""
    from macos_bridge import apps

    apps.bundle_id_to_app_name.cache_clear()
    monkeypatch.setattr(
        "macos_bridge.phases.per_app.bundle_id_to_app_name",
        lambda bid: {
            "com.apple.Safari": "Safari",
            "com.apple.logic10": "Logic Pro",
            "com.tinyspeck.slackmacgap": "Slack",
        }.get(bid, bid),
    )


def _ticker(knowledge_fixture: Path, apps: list[AppEntry]) -> PerAppTicker:
    return PerAppTicker(
        enabled=True,
        interval_seconds=60,
        knowledge_db_path=knowledge_fixture,
        tmp_dir=knowledge_fixture.parent / "work",
        host_slug="test_host",
        topic_prefix="macos",
        availability_topic="macos/test_host/status",
        apps=apps,
        local_timezone=ZoneInfo("UTC"),
    )


def test_phase_b_publishes_one_state_per_allow_listed_app(
    knowledge_fixture: Path,
    anchor_today: datetime,
    fake_mqtt: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
):
    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return anchor_today.astimezone(tz)

    monkeypatch.setattr("macos_bridge.time_utils.datetime", FrozenDatetime)

    ticker = _ticker(
        knowledge_fixture,
        [
            AppEntry(bundle_id="com.apple.Safari", friendly_name="Safari"),
            AppEntry(bundle_id="com.apple.logic10", friendly_name="Logic Pro"),
            AppEntry(bundle_id="com.tinyspeck.slackmacgap", friendly_name="Slack"),
            AppEntry(bundle_id="com.app.never-used", friendly_name="Never Used"),
        ],
    )
    asyncio.run(ticker.run_once(fake_mqtt))

    publishes = {c.args[0]: c.args[1] for c in fake_mqtt.publish_state.call_args_list}
    base = "macos/test_host/today/apps"
    # Values converted from minutes → hours (rounded to 2 decimals)
    assert publishes[f"{base}/com.apple.Safari"] == 0.5    # 30 min
    assert publishes[f"{base}/com.apple.logic10"] == 0.75  # 45 min
    assert publishes[f"{base}/com.tinyspeck.slackmacgap"] == 0.25  # 15 min
    # Apps in the allow-list with no usage today still publish 0
    assert publishes[f"{base}/com.app.never-used"] == 0


def test_phase_b_publishes_discovery_per_app(knowledge_fixture: Path, fake_mqtt: MagicMock):
    apps = [
        AppEntry(bundle_id="com.apple.Safari", friendly_name="Safari"),
        AppEntry(bundle_id="com.tinyspeck.slackmacgap"),  # falls back to friendly lookup
    ]
    ticker = _ticker(knowledge_fixture, apps)
    asyncio.run(ticker.publish_discovery(fake_mqtt))
    assert fake_mqtt.publish_discovery.call_count == 2
    unique_ids = {c.kwargs["unique_id"] for c in fake_mqtt.publish_discovery.call_args_list}
    assert unique_ids == {
        "macos_test_host_today_app_com_apple_safari",
        "macos_test_host_today_app_com_tinyspeck_slackmacgap",
    }


def test_phase_b_no_op_when_allow_list_empty(knowledge_fixture: Path, fake_mqtt: MagicMock):
    ticker = _ticker(knowledge_fixture, [])
    asyncio.run(ticker.run_once(fake_mqtt))
    assert fake_mqtt.publish_state.call_count == 0


def test_phase_b_uses_per_app_icon_when_provided(
    knowledge_fixture: Path, fake_mqtt: MagicMock
):
    apps = [
        AppEntry(bundle_id="com.apple.Music", friendly_name="Music", icon="mdi:music"),
        AppEntry(bundle_id="com.apple.Safari", friendly_name="Safari"),  # default
    ]
    ticker = _ticker(knowledge_fixture, apps)
    asyncio.run(ticker.publish_discovery(fake_mqtt))
    icons_by_bundle: dict[str, str] = {}
    for call in fake_mqtt.publish_discovery.call_args_list:
        uid = call.kwargs["unique_id"]
        icons_by_bundle[uid] = call.kwargs["payload"]["icon"]
    assert icons_by_bundle["macos_test_host_today_app_com_apple_music"] == "mdi:music"
    assert icons_by_bundle["macos_test_host_today_app_com_apple_safari"] == "mdi:application"


def test_phase_b_uses_hours_unit_in_discovery(
    knowledge_fixture: Path, fake_mqtt: MagicMock
):
    apps = [AppEntry(bundle_id="com.apple.Safari", friendly_name="Safari")]
    ticker = _ticker(knowledge_fixture, apps)
    asyncio.run(ticker.publish_discovery(fake_mqtt))
    payload = fake_mqtt.publish_discovery.call_args_list[0].kwargs["payload"]
    assert payload["unit_of_measurement"] == "h"
    assert payload["device_class"] == "duration"
