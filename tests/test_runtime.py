"""Tests for the merged Bridge runtime's per-ticker supervision."""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest

from macos_bridge.config import Config
from macos_bridge.phases.base import AbstractTicker
from macos_bridge.runtime import Bridge


class _CountingTicker(AbstractTicker):
    name = "counting"

    def __init__(self, enabled: bool = True):
        self._enabled = enabled
        self.tick_count = 0
        self.discovery_published = False

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def interval_seconds(self) -> int | None:
        return 1

    async def run_once(self, mqtt) -> None:
        self.tick_count += 1

    async def publish_discovery(self, mqtt) -> None:
        self.discovery_published = True


class _BrokenTicker(AbstractTicker):
    name = "broken"

    @property
    def enabled(self) -> bool:
        return True

    @property
    def interval_seconds(self) -> int | None:
        return 1

    async def run_once(self, mqtt) -> None:
        raise RuntimeError("intentional")


def _minimal_config(ca_file: str = "/etc/ssl/cert.pem") -> Config:
    return Config.model_validate({
        "bridge": {"hostname": "test_host"},
        "mqtt": {
            "host": "broker.lan",
            "port": 8883,
            "tls": True,
            "ca_file": ca_file,
        },
    })


def _write_self_signed_ca(dir_path) -> str:
    """Generate a throwaway self-signed cert and return its path.

    The non-dry-run publisher calls paho ``tls_set()`` eagerly at
    construction, which ``load_verify_locations()`` the ca_file. The repo's
    A Linux CI host has no ``/etc/ssl/cert.pem`` (macOS does), so hardcoding it
    makes this test environment-dependent. openssl is present on both
    platforms; a fresh self-signed cert loads cleanly anywhere.
    """
    import subprocess

    cert = dir_path / "ca.pem"
    key = dir_path / "key.pem"
    subprocess.run(
        [
            "openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
            "-keyout", str(key), "-out", str(cert), "-days", "1",
            "-subj", "/CN=macos-bridge-test",
        ],
        check=True, capture_output=True,
    )
    return str(cert)


@pytest.fixture
def bridge() -> Bridge:
    """Construct a dry-run Bridge so no real MQTT connection is created."""
    cfg = _minimal_config()
    return Bridge(cfg, creds=None, host_slug="test_host", dry_run=True)


async def test_phase_loop_invokes_run_once_repeatedly(bridge: Bridge):
    ticker = _CountingTicker(enabled=True)
    bridge.publisher = MagicMock()  # _run_phase_loop passes this through

    task = asyncio.create_task(bridge._run_phase_loop(ticker))
    await asyncio.sleep(2.5)  # ~3 ticks at 1s interval (1 immediate + 2 scheduled)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    assert ticker.tick_count >= 2
    assert ticker.discovery_published is True


async def test_phase_loop_swallows_exceptions(bridge: Bridge):
    """A broken ticker shouldn't tear down the loop or interrupt sibling tickers."""
    broken = _BrokenTicker()
    bridge.publisher = MagicMock()

    task = asyncio.create_task(bridge._run_phase_loop(broken))
    await asyncio.sleep(2.5)
    assert not task.done()  # still iterating despite the raised RuntimeError
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


def test_dry_run_bridge_has_no_publisher_or_outbox():
    cfg = _minimal_config()
    b = Bridge(cfg, creds=None, host_slug="test_host", dry_run=True)
    assert b.publisher is None
    assert b.outbox is None
    # Sources are still constructed so init_state and dump-once work.
    assert len(b.sources) >= 1


def test_real_bridge_builds_publisher_and_outbox(
    monkeypatch: pytest.MonkeyPatch, tmp_path
):
    monkeypatch.setenv("MQTT_USERNAME", "u")
    monkeypatch.setenv("MQTT_PASSWORD", "p")
    cfg = _minimal_config(ca_file=_write_self_signed_ca(tmp_path))
    from macos_bridge.config import LoadedCredentials

    creds = LoadedCredentials(username="u", password="p")
    b = Bridge(cfg, creds=creds, host_slug="test_host")
    assert b.publisher is not None
    assert b.outbox is not None
    # 8 Phase tickers (aggregates, per_app, focused_app, brave_history,
    # family, activity, system_state, host_metrics) + today_counters
    # + virtual_meetings + permissions + focus_mode + unread_messages
    # + software_updates + tailscale + time_machine + displays
    # + security_posture + now_playing + system_info + location = 21.
    assert len(b.tickers) == 21


def test_seed_state_mirrors_publishes_one_per_event_path(bridge: Bridge):
    """The seed pass should publish at most one retained state-mirror update
    per HA entity, taking the FIRST occurrence as it walks each source's
    iter_recent() output. Repeats of the same event_path are ignored."""
    from macos_bridge.discovery import CommsHADiscovery

    bridge.publisher = MagicMock()
    bridge.publisher.publish_event_with_ack = MagicMock(return_value=True)
    bridge.publisher.host_device_block = MagicMock(return_value={"identifiers": ["x"]})
    bridge.comms_discovery = CommsHADiscovery(
        cfg=bridge.cfg,
        hostname="test_host",
        device_block=bridge.publisher.host_device_block(),
    )

    class _FakeSource:
        def iter_recent(self, limit):
            # Two sent + two received in newest-first order; only the first
            # of each event_path should be seeded.
            yield "messages/sent", {"rowid": 5, "handle": "+16125550101"}
            yield "messages/received", {"rowid": 4, "handle": "+16125550102"}
            yield "messages/sent", {"rowid": 3, "handle": "+16125550103"}
            yield "messages/received", {"rowid": 2, "handle": "+16125550104"}

    bridge.sources = [_FakeSource()]
    bridge._seed_state_mirrors(lookback=10)

    calls = bridge.publisher.publish_event_with_ack.call_args_list
    topics = [c.args[0] for c in calls]
    payloads = [c.args[1] for c in calls]

    # Exactly two state-mirror publishes (one per event_path) — the latest
    # rowid wins because iter_recent yields newest first.
    assert sorted(topics) == sorted([
        "macos/test_host/state/last_message_sent",
        "macos/test_host/state/last_message_received",
    ])
    rowid_by_topic = dict(zip(topics, [p["rowid"] for p in payloads]))
    assert rowid_by_topic["macos/test_host/state/last_message_sent"] == 5
    assert rowid_by_topic["macos/test_host/state/last_message_received"] == 4

    # Every published payload carries a host attribute (matches _emit shape).
    assert all(p.get("host") == "test_host" for p in payloads)
    # All publishes use retain=True so the value survives a bridge restart.
    assert all(c.kwargs.get("retain") is True for c in calls)


def test_seed_state_mirrors_no_publisher_is_safe(bridge: Bridge):
    """When publisher is None (dry-run) the seed pass should be a no-op."""
    bridge.publisher = None
    bridge.comms_discovery = None
    bridge._seed_state_mirrors()  # must not raise
