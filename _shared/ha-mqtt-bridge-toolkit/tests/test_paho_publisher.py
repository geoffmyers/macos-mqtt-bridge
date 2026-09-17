"""Tests for ha_mqtt_bridge.paho_publisher.

We mock paho's Client class so the tests run without a real broker.
The behavior we pin is the wrapper's wire-level contract:

  - Constructor wires LWT (only when lwt_topic is set), TLS (only when
    tls=True), and username/password (only when supplied).
  - publish_state / publish_attributes / publish_event / publish_discovery
    emit on the correct topic with the configured QoS + retain.
  - on_connect publishes lwt_online retained and re-applies subscriptions.
  - on_disconnect clears the connected flag.
  - publish_event_with_ack returns False when not connected.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from ha_mqtt_bridge.paho_publisher import ThreadedPublisher, watch_ha_birth


@pytest.fixture
def fake_paho_client():
    """Replace paho.Client with a MagicMock; yield the instance + ctor mock."""
    with patch("ha_mqtt_bridge.paho_publisher.paho.Client") as ctor:
        instance = MagicMock()
        # publish() returns a paho MQTTMessageInfo-ish object — give it a
        # wait_for_publish + is_published default so the ack path can return
        # success unless a test overrides it.
        info = MagicMock()
        info.wait_for_publish.return_value = None
        info.is_published.return_value = True
        instance.publish.return_value = info
        ctor.return_value = instance
        yield instance


def _make_publisher(**overrides: Any) -> ThreadedPublisher:
    defaults = dict(
        host="broker.local",
        port=1883,
        client_id="test",
        lwt_topic="bridge/online",
    )
    defaults.update(overrides)
    return ThreadedPublisher(**defaults)


class TestConstructor:
    def test_minimal(self, fake_paho_client):
        _make_publisher()
        # LWT was set
        fake_paho_client.will_set.assert_called_once_with(
            "bridge/online", payload="offline", qos=1, retain=True
        )
        # No TLS, no auth
        fake_paho_client.tls_set.assert_not_called()
        fake_paho_client.username_pw_set.assert_not_called()

    def test_auth(self, fake_paho_client):
        _make_publisher(username="u", password="p")
        fake_paho_client.username_pw_set.assert_called_once_with("u", "p")

    def test_auth_empty_password_ok(self, fake_paho_client):
        _make_publisher(username="u", password=None)
        fake_paho_client.username_pw_set.assert_called_once_with("u", "")

    def test_no_lwt(self, fake_paho_client):
        _make_publisher(lwt_topic=None)
        fake_paho_client.will_set.assert_not_called()

    def test_tls_set(self, fake_paho_client):
        _make_publisher(tls=True, ca_file="/etc/ssl/cert.pem")
        fake_paho_client.tls_set.assert_called_once()
        kwargs = fake_paho_client.tls_set.call_args.kwargs
        assert kwargs["ca_certs"] == "/etc/ssl/cert.pem"

    def test_tls_no_ca_file(self, fake_paho_client):
        # Passing tls=True without ca_file uses system trust store
        # (ca_certs=None handed straight to paho).
        _make_publisher(tls=True)
        fake_paho_client.tls_set.assert_called_once()
        assert fake_paho_client.tls_set.call_args.kwargs["ca_certs"] is None


class TestLifecycle:
    def test_start_connects_async_and_loop_starts(self, fake_paho_client):
        p = _make_publisher()
        p.start()
        fake_paho_client.connect_async.assert_called_once_with(
            "broker.local", 1883, keepalive=60
        )
        fake_paho_client.loop_start.assert_called_once()

    def test_stop_publishes_offline_and_stops_loop(self, fake_paho_client):
        p = _make_publisher()
        p.start()
        p.stop()
        # The offline-LWT publish before disconnect
        publish_calls = [
            c for c in fake_paho_client.publish.call_args_list
            if c.args[0] == "bridge/online"
        ]
        assert any(
            c.kwargs.get("payload") == "offline" for c in publish_calls
        )
        fake_paho_client.loop_stop.assert_called_once()
        fake_paho_client.disconnect.assert_called_once()

    def test_stop_with_no_lwt_skips_offline_publish(self, fake_paho_client):
        p = _make_publisher(lwt_topic=None)
        p.start()
        before = len(fake_paho_client.publish.call_args_list)
        p.stop()
        # No new publish after stop()
        assert len(fake_paho_client.publish.call_args_list) == before

    def test_is_connected_starts_false(self, fake_paho_client):
        p = _make_publisher()
        assert p.is_connected() is False


class TestOnConnect:
    def test_success_publishes_online_and_reapplies_subscriptions(
        self, fake_paho_client
    ):
        p = _make_publisher()
        p.subscribe("control/foo", lambda t, b: None)
        # Simulate paho calling on_connect with success (reason_code=0).
        # We grab the on_connect callback that the publisher attached.
        on_connect = fake_paho_client.on_connect
        # Constructor assigned a function to fake_paho_client.on_connect;
        # MagicMock-on-attribute assignment stores it. Invoke it.
        on_connect(fake_paho_client, None, {}, MagicMock(value=0))
        # connected flag set
        assert p.is_connected() is True
        # online published, retained, on event_qos
        online_call = next(
            c for c in fake_paho_client.publish.call_args_list
            if c.args[0] == "bridge/online" and c.kwargs.get("payload") == "online"
        )
        assert online_call.kwargs["qos"] == 1
        assert online_call.kwargs["retain"] is True
        # subscription replayed
        fake_paho_client.subscribe.assert_called_with("control/foo", qos=1)

    def test_failure_keeps_disconnected(self, fake_paho_client):
        p = _make_publisher()
        on_connect = fake_paho_client.on_connect
        on_connect(fake_paho_client, None, {}, MagicMock(value=1))
        assert p.is_connected() is False


class TestPublishFlavors:
    def test_publish_event_emits_json_with_event_qos(self, fake_paho_client):
        p = _make_publisher()
        p.publish_event("a/b", {"k": 1})
        call = fake_paho_client.publish.call_args
        assert call.args[0] == "a/b"
        assert json.loads(call.kwargs["payload"]) == {"k": 1}
        assert call.kwargs["qos"] == 1
        assert call.kwargs["retain"] is False  # retain_events default

    def test_publish_event_retain_override(self, fake_paho_client):
        p = _make_publisher()
        p.publish_event("a/b", {"k": 1}, retain=True)
        assert fake_paho_client.publish.call_args.kwargs["retain"] is True

    def test_publish_state_emits_string_retained(self, fake_paho_client):
        p = _make_publisher()
        p.publish_state("state/x", 42)
        call = fake_paho_client.publish.call_args
        assert call.args[0] == "state/x"
        assert call.args[1] == "42"
        assert call.kwargs["qos"] == 0  # state_qos default
        assert call.kwargs["retain"] is True

    def test_publish_attributes_emits_sorted_json(self, fake_paho_client):
        p = _make_publisher()
        p.publish_attributes("attrs/x", {"b": 2, "a": 1})
        body = fake_paho_client.publish.call_args.args[1]
        # sort_keys=True
        assert body == '{"a": 1, "b": 2}'

    def test_publish_discovery_uses_discovery_prefix(self, fake_paho_client):
        p = _make_publisher(discovery_prefix="ha")
        p.publish_discovery(
            component="sensor",
            unique_id="my_sensor",
            payload={"name": "X"},
        )
        call = fake_paho_client.publish.call_args
        assert call.args[0] == "ha/sensor/my_sensor/config"
        assert call.kwargs["retain"] is True
        assert call.kwargs["qos"] == 1  # discovery_qos default
        assert json.loads(call.args[1]) == {"name": "X"}

    def test_publish_raw_bypasses_defaults(self, fake_paho_client):
        p = _make_publisher()
        p.publish_raw("ha/sensor/x/config", "", qos=1, retain=True)
        call = fake_paho_client.publish.call_args
        assert call.args == ("ha/sensor/x/config", "")
        assert call.kwargs == {"qos": 1, "retain": True}

    def test_publish_event_with_ack_returns_false_when_disconnected(
        self, fake_paho_client
    ):
        p = _make_publisher()
        # _connected event never set
        assert p.publish_event_with_ack("a/b", {"k": 1}) is False
        # And no publish was issued
        # (one publish call may have happened for LWT — filter out)
        non_lwt = [
            c for c in fake_paho_client.publish.call_args_list
            if c.args[0] != "bridge/online"
        ]
        assert non_lwt == []

    def test_publish_event_with_ack_success_when_connected(
        self, fake_paho_client
    ):
        p = _make_publisher()
        # Manually set connected so we skip the on_connect callback
        p._connected.set()
        assert p.publish_event_with_ack("a/b", {"k": 1}) is True

    def test_publish_event_with_ack_timeout_returns_false(self, fake_paho_client):
        p = _make_publisher()
        p._connected.set()
        info = MagicMock()
        info.wait_for_publish.side_effect = RuntimeError("timeout")
        fake_paho_client.publish.return_value = info
        assert p.publish_event_with_ack("a/b", {"k": 1}, timeout=0.1) is False


class TestSubscribe:
    def test_subscribe_before_connect_defers(self, fake_paho_client):
        p = _make_publisher()
        p.subscribe("a/b", lambda t, body: None)
        # Not subscribed yet because not connected
        fake_paho_client.subscribe.assert_not_called()

    def test_subscribe_after_connect_immediate(self, fake_paho_client):
        p = _make_publisher()
        p._connected.set()
        p.subscribe("a/b", lambda t, body: None)
        fake_paho_client.subscribe.assert_called_once_with("a/b", qos=1)

    def test_on_message_dispatches_to_handler(self, fake_paho_client):
        p = _make_publisher()
        seen = []
        p.subscribe("a/b", lambda t, body: seen.append((t, body)))
        msg = MagicMock(topic="a/b", payload=b"hi")
        on_message = fake_paho_client.on_message
        on_message(fake_paho_client, None, msg)
        assert seen == [("a/b", b"hi")]

    def test_on_message_unsubscribed_topic_ignored(self, fake_paho_client):
        p = _make_publisher()
        msg = MagicMock(topic="unknown", payload=b"hi")
        # Should not raise
        fake_paho_client.on_message(fake_paho_client, None, msg)

    def test_on_message_handler_exception_does_not_crash(
        self, fake_paho_client, caplog
    ):
        p = _make_publisher()

        def bad_handler(t, body):
            raise RuntimeError("boom")

        p.subscribe("a/b", bad_handler)
        msg = MagicMock(topic="a/b", payload=b"hi")
        # Should not raise — logged via logger.exception
        fake_paho_client.on_message(fake_paho_client, None, msg)


class TestDisconnect:
    def test_on_disconnect_clears_connected(self, fake_paho_client):
        p = _make_publisher()
        p._connected.set()
        fake_paho_client.on_disconnect(fake_paho_client, None, {}, MagicMock(value=0))
        assert p.is_connected() is False


class TestHealthHeartbeat:
    """Every successful publish_* call bumps health_path's mtime — but
    ONLY while actually connected to the broker, so a healthcheck that
    just stats the file's mtime can't stay green while the connection
    is down (the 2026-09-17 bug: the bridge's own polling cadence kept
    advancing the mtime regardless of whether anything reached a
    broker, because paho silently drops a publish() call issued while
    disconnected — no exception, nothing to catch)."""

    def test_disabled_by_default(self, fake_paho_client, tmp_path):
        p = _make_publisher()
        p._connected.set()
        target = tmp_path / "healthy"
        p.publish_state("t/x", "1")
        assert not target.exists()

    def test_state_bumps_mtime_when_connected(self, fake_paho_client, tmp_path):
        target = tmp_path / "healthy"
        p = _make_publisher(health_path=str(target))
        p._connected.set()
        p.publish_state("t/x", "1")
        assert target.exists()

    def test_state_does_not_bump_when_disconnected(self, fake_paho_client, tmp_path):
        target = tmp_path / "healthy"
        p = _make_publisher(health_path=str(target))
        # _connected never set — simulates the broker being down while the
        # bridge's poll loop keeps calling publish_state() anyway.
        p.publish_state("t/x", "1")
        assert not target.exists()

    def test_event_bumps_mtime_when_connected(self, fake_paho_client, tmp_path):
        target = tmp_path / "healthy"
        p = _make_publisher(health_path=str(target))
        p._connected.set()
        p.publish_event("t/e", {"a": 1})
        assert target.exists()

    def test_event_does_not_bump_when_disconnected(self, fake_paho_client, tmp_path):
        target = tmp_path / "healthy"
        p = _make_publisher(health_path=str(target))
        p.publish_event("t/e", {"a": 1})
        assert not target.exists()

    def test_discovery_bumps_mtime_when_connected(self, fake_paho_client, tmp_path):
        target = tmp_path / "healthy"
        p = _make_publisher(health_path=str(target))
        p._connected.set()
        p.publish_discovery(component="sensor", unique_id="u", payload={"x": 1})
        assert target.exists()

    def test_discovery_does_not_bump_when_disconnected(self, fake_paho_client, tmp_path):
        target = tmp_path / "healthy"
        p = _make_publisher(health_path=str(target))
        p.publish_discovery(component="sensor", unique_id="u", payload={"x": 1})
        assert not target.exists()

    def test_attributes_bumps_mtime_when_connected(self, fake_paho_client, tmp_path):
        target = tmp_path / "healthy"
        p = _make_publisher(health_path=str(target))
        p._connected.set()
        p.publish_attributes("t/a", {"k": "v"})
        assert target.exists()

    def test_attributes_does_not_bump_when_disconnected(self, fake_paho_client, tmp_path):
        target = tmp_path / "healthy"
        p = _make_publisher(health_path=str(target))
        p.publish_attributes("t/a", {"k": "v"})
        assert not target.exists()

    def test_raw_bumps_mtime_when_connected(self, fake_paho_client, tmp_path):
        target = tmp_path / "healthy"
        p = _make_publisher(health_path=str(target))
        p._connected.set()
        p.publish_raw("t/r", b"")
        assert target.exists()

    def test_raw_does_not_bump_when_disconnected(self, fake_paho_client, tmp_path):
        target = tmp_path / "healthy"
        p = _make_publisher(health_path=str(target))
        p.publish_raw("t/r", b"")
        assert not target.exists()

    def test_ack_failure_does_not_bump(self, fake_paho_client, tmp_path):
        target = tmp_path / "healthy"
        p = _make_publisher(health_path=str(target))
        p._connected.set()
        info = MagicMock()
        info.wait_for_publish.return_value = None
        info.is_published.return_value = False
        fake_paho_client.publish.return_value = info
        ok = p.publish_event_with_ack("t/e", {"a": 1})
        assert ok is False
        assert not target.exists()

    def test_ack_disconnected_does_not_bump(self, fake_paho_client, tmp_path):
        target = tmp_path / "healthy"
        p = _make_publisher(health_path=str(target))
        # _connected stays unset → publish_event_with_ack returns False fast
        assert p.publish_event_with_ack("t/e", {"a": 1}) is False
        assert not target.exists()

    def test_touch_failure_swallowed(self, fake_paho_client, tmp_path):
        # Read-only directory: opening for append raises OSError, which
        # the publisher must swallow rather than blowing up the publish.
        p = _make_publisher(health_path=str(tmp_path / "no/such/dir/healthy"))
        p._connected.set()
        p.publish_state("t/x", "1")  # must not raise

    def test_reconnect_touches_health_immediately(self, fake_paho_client, tmp_path):
        # A successful on_connect callback is itself proof of life —
        # the mtime should be fresh right after a reconnect, not only
        # after the next publish_* call.
        target = tmp_path / "healthy"
        p = _make_publisher(health_path=str(target))
        on_connect = fake_paho_client.on_connect
        on_connect(fake_paho_client, None, {}, MagicMock(value=0))
        assert target.exists()

    def test_failed_connect_does_not_touch_health(self, fake_paho_client, tmp_path):
        target = tmp_path / "healthy"
        p = _make_publisher(health_path=str(target))
        on_connect = fake_paho_client.on_connect
        on_connect(fake_paho_client, None, {}, MagicMock(value=1))
        assert not target.exists()


class TestWatchHaBirth:
    """`watch_ha_birth` — opt-in re-publish of discovery (+ bridge state)
    when HA's own `<discovery_prefix>/status` reports `"online"`."""

    def test_subscribes_to_default_status_topic(self, fake_paho_client):
        p = _make_publisher()
        watch_ha_birth(p, lambda: None)
        assert f"{p.discovery_prefix}/status" in p._subscriptions

    def test_custom_discovery_prefix(self, fake_paho_client):
        p = _make_publisher(discovery_prefix="ha")
        watch_ha_birth(p, lambda: None, discovery_prefix="ha")
        assert "ha/status" in p._subscriptions

    def test_online_payload_invokes_callback(self, fake_paho_client):
        p = _make_publisher()
        calls = []
        watch_ha_birth(p, lambda: calls.append(1))
        handler = p._subscriptions["homeassistant/status"]
        handler("homeassistant/status", b"online")
        assert calls == [1]

    def test_offline_payload_does_not_invoke_callback(self, fake_paho_client):
        p = _make_publisher()
        calls = []
        watch_ha_birth(p, lambda: calls.append(1))
        handler = p._subscriptions["homeassistant/status"]
        handler("homeassistant/status", b"offline")
        assert calls == []

    def test_callback_exception_does_not_propagate(self, fake_paho_client):
        p = _make_publisher()

        def boom():
            raise RuntimeError("boom")

        watch_ha_birth(p, boom)
        handler = p._subscriptions["homeassistant/status"]
        handler("homeassistant/status", b"online")  # must not raise

    def test_custom_birth_payload(self, fake_paho_client):
        p = _make_publisher()
        calls = []
        watch_ha_birth(p, lambda: calls.append(1), birth_payload="up")
        handler = p._subscriptions["homeassistant/status"]
        handler("homeassistant/status", b"online")
        assert calls == []
        handler("homeassistant/status", b"up")
        assert calls == [1]
