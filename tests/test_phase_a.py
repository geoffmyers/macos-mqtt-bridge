# tests/test_phase_a.py
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

import pytest

from macos_bridge.phases.aggregates import AggregatesTicker


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
        "macos_bridge.phases.aggregates.bundle_id_to_app_name",
        lambda bid: {"com.apple.logic10": "Logic Pro"}.get(bid, bid),
    )


def test_phase_a_publishes_four_state_sensors(
    knowledge_fixture: Path,
    anchor_today: datetime,
    fake_mqtt: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
):
    # Pin "now" to noon UTC on the fixture's anchor date so start_of_today_mat lines up
    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return anchor_today.astimezone(tz)

    monkeypatch.setattr("macos_bridge.time_utils.datetime", FrozenDatetime)

    ticker = AggregatesTicker(
        enabled=True,
        interval_seconds=900,
        knowledge_db_path=knowledge_fixture,
        tmp_dir=knowledge_fixture.parent / "work",
        host_slug="test_host",
        topic_prefix="macos",
        availability_topic="macos/test_host/status",
        local_timezone=ZoneInfo("UTC"),
    )

    import asyncio

    asyncio.run(ticker.run_once(fake_mqtt))

    publish_state_calls = {c.args[0]: c.args[1] for c in fake_mqtt.publish_state.call_args_list}
    # Today's total: 30 (Safari) + 15 (Slack) + 45 (Logic) + 10 (WindowManager)
    # + 10 (helper) = 110 min ≈ 1.83 h. Phase A includes ALL /app/usage rows;
    # the bootstrap-allowlist filter is what drops daemons/helpers.
    assert publish_state_calls["macos/test_host/today/total"] == 1.83
    # Pickups: 7
    assert publish_state_calls["macos/test_host/today/pickups"] == 7
    # Top app: Logic Pro friendly name (45 min ≈ 0.75 h)
    assert publish_state_calls["macos/test_host/today/top_app"] == "Logic Pro"
    assert publish_state_calls["macos/test_host/today/top_app_minutes"] == 0.75

    # Bundle ID is published as an attribute on the top_app sensor
    attrs_calls = {c.args[0]: c.args[1] for c in fake_mqtt.publish_attributes.call_args_list}
    assert attrs_calls["macos/test_host/today/top_app/attrs"] == {
        "bundle_id": "com.apple.logic10"
    }


def test_phase_a_publishes_four_discovery_configs(knowledge_fixture: Path, fake_mqtt: MagicMock):
    ticker = AggregatesTicker(
        enabled=True,
        interval_seconds=900,
        knowledge_db_path=knowledge_fixture,
        tmp_dir=knowledge_fixture.parent / "work",
        host_slug="test_host",
        topic_prefix="macos",
        availability_topic="macos/test_host/status",
        local_timezone=ZoneInfo("UTC"),
    )

    import asyncio

    asyncio.run(ticker.publish_discovery(fake_mqtt))
    assert fake_mqtt.publish_discovery.call_count == 4
    unique_ids = {c.kwargs["unique_id"] for c in fake_mqtt.publish_discovery.call_args_list}
    assert unique_ids == {
        "macos_test_host_today_total",
        "macos_test_host_today_pickups",
        "macos_test_host_today_top_app",
        "macos_test_host_today_top_app_minutes",
    }
