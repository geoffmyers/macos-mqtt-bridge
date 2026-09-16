"""Tests for Phase D synced cross-device Screen Time ticker."""

from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

import pytest

from macos_bridge.config import FamilyMember
from macos_bridge.phases.family import FamilyTicker


@pytest.fixture
def fake_mqtt() -> MagicMock:
    m = MagicMock()
    m.sw_version = "0.1.0"
    m.host_friendly_name = "Test Host"
    m._client = MagicMock()
    return m


@pytest.fixture(autouse=True)
def _stub_bundle_lookup(monkeypatch: pytest.MonkeyPatch):
    from macos_bridge import apps

    apps.bundle_id_to_app_name.cache_clear()
    monkeypatch.setattr(
        "macos_bridge.phases.family.bundle_id_to_app_name",
        lambda bid: {
            "com.apple.Safari": "Safari",
            "com.apple.logic10": "Logic Pro",
        }.get(bid, bid or ""),
    )


def _ticker(
    rmadmin_cloud_fixture: Path,
    *,
    organizer: bool = True,
    members: list[FamilyMember] | None = None,
) -> FamilyTicker:
    return FamilyTicker(
        enabled=organizer,
        is_family_organizer=organizer,
        interval_seconds=300,
        rm_admin_local_path=rmadmin_cloud_fixture,  # fixture name kept for compat
        tmp_dir=rmadmin_cloud_fixture.parent / "work",
        topic_prefix="macos",
        availability_topic="macos/test_host/status",
        member_filter=members,
        local_timezone=ZoneInfo("UTC"),
    )


def test_phase_d_disabled_when_not_organizer(rmadmin_cloud_fixture: Path):
    ticker = _ticker(rmadmin_cloud_fixture, organizer=False)
    assert ticker.enabled is False


def test_phase_d_publishes_per_pair_totals(
    rmadmin_cloud_fixture: Path,
    anchor_today: datetime,
    fake_mqtt: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
):
    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return anchor_today.astimezone(tz)

    monkeypatch.setattr("macos_bridge.time_utils.datetime", FrozenDatetime)

    ticker = _ticker(rmadmin_cloud_fixture)
    asyncio.run(ticker.run_once(fake_mqtt))

    publishes = {c.args[0]: c.args[1] for c in fake_mqtt.publish_state.call_args_list}
    base = "macos/family"
    # Totals converted from minutes → hours (rounded to 2 decimals)
    assert publishes[f"{base}/alex/alexs_macbook_pro/today/total"] == 1.5  # 90 min
    assert publishes[f"{base}/alex/alexs_macbook_pro/today/pickups"] == 5
    assert publishes[f"{base}/alex/alexs_iphone/today/total"] == 0.75  # 45 min
    assert publishes[f"{base}/alex/alexs_iphone/today/pickups"] == 12
    assert publishes[f"{base}/alex/alexs_apple_watch/today/total"] == 0.08  # 5 min
    assert publishes[f"{base}/jordan/jordans_ipad/today/total"] == 2.0  # 120 min
    assert publishes[f"{base}/jordan/jordans_ipad/today/pickups"] == 20


def test_phase_d_publishes_top_app_across_devices(
    rmadmin_cloud_fixture: Path,
    anchor_today: datetime,
    fake_mqtt: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
):
    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return anchor_today.astimezone(tz)

    monkeypatch.setattr("macos_bridge.time_utils.datetime", FrozenDatetime)

    ticker = _ticker(rmadmin_cloud_fixture)
    asyncio.run(ticker.run_once(fake_mqtt))

    publishes = {c.args[0]: c.args[1] for c in fake_mqtt.publish_state.call_args_list}
    assert publishes["macos/family/top_app/today"] == "Safari"

    attrs = {c.args[0]: c.args[1] for c in fake_mqtt.publish_attributes.call_args_list}
    payload = attrs["macos/family/top_app/today/attrs"]
    assert payload["bundle_id"] == "com.apple.Safari"
    assert payload["hours"] == 1.25  # 75 min
    bundle_ids = [a["bundle_id"] for a in payload["all"]]
    assert "com.apple.Safari" in bundle_ids


def test_phase_d_member_filter_drops_non_listed_users(
    rmadmin_cloud_fixture: Path,
    anchor_today: datetime,
    fake_mqtt: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
):
    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return anchor_today.astimezone(tz)

    monkeypatch.setattr("macos_bridge.time_utils.datetime", FrozenDatetime)

    ticker = _ticker(rmadmin_cloud_fixture, members=[FamilyMember(dsid=222, slug="jordan")])
    asyncio.run(ticker.run_once(fake_mqtt))

    publishes = {c.args[0]: c.args[1] for c in fake_mqtt.publish_state.call_args_list}
    assert any("jordan" in t for t in publishes)
    assert not any(
        "alex" in t for t in publishes if t.startswith("macos/family")
    )


def test_phase_d_publishes_one_discovery_per_pair_plus_top_app(
    rmadmin_cloud_fixture: Path, fake_mqtt: MagicMock
):
    ticker = _ticker(rmadmin_cloud_fixture)
    asyncio.run(ticker.publish_discovery(fake_mqtt))
    # 4 (user, device) pairs × 2 sensors each + 1 top-app sensor
    assert fake_mqtt.publish_discovery.call_count == 9
    unique_ids = {c.kwargs["unique_id"] for c in fake_mqtt.publish_discovery.call_args_list}
    assert "macos_family_top_app_today" in unique_ids
    assert "macos_family_alex_alexs_macbook_pro_today_total" in unique_ids
    assert "macos_family_alex_alexs_iphone_today_pickups" in unique_ids
    assert "macos_family_jordan_jordans_ipad_today_total" in unique_ids


def test_phase_d_soft_fails_on_schema_mismatch(tmp_path: Path, fake_mqtt: MagicMock):
    """When the source DB exists but tables are missing, no crash; no publishes."""
    import sqlite3

    bogus = tmp_path / "RMAdminStore-Local.sqlite"
    sqlite3.connect(bogus).close()
    ticker = FamilyTicker(
        enabled=True,
        is_family_organizer=True,
        interval_seconds=300,
        rm_admin_local_path=bogus,
        tmp_dir=tmp_path / "work",
        topic_prefix="macos",
        availability_topic="macos/test_host/status",
        member_filter=None,
    )
    asyncio.run(ticker.run_once(fake_mqtt))
    state_calls = [c for c in fake_mqtt.publish_state.call_args_list if "family/" in c.args[0]]
    assert state_calls == []
