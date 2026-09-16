# tests/test_smoke_e2e.py
"""End-to-end smoke test against an ephemeral Mosquitto + the synthetic knowledgeC fixture.

Skipped when Docker is unavailable.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import socket
import subprocess
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import paho.mqtt.client as paho
import pytest

from macos_bridge.config import MqttConfig
from macos_bridge.mqtt import MqttPublisher
from macos_bridge.phases.aggregates import AggregatesTicker


def _docker_available() -> bool:
    """Return True only when the docker binary exists AND the daemon is reachable."""
    if shutil.which("docker") is None:
        return False
    result = subprocess.run(["docker", "info"], capture_output=True, timeout=5)
    return result.returncode == 0


pytestmark = pytest.mark.skipif(
    not _docker_available(), reason="docker not available or daemon not running"
)


def _free_port() -> int:
    s = socket.socket()
    s.bind(("", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture
def mosquitto():
    """Start a one-off mosquitto in Docker, yield (host, port), tear down."""
    port = _free_port()
    name = f"screen-time-bridge-smoke-{port}"
    subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "-d",
            "--name",
            name,
            "-p",
            f"{port}:1883",
            "eclipse-mosquitto:2.0",
            "mosquitto",
            "-c",
            "/mosquitto-no-auth.conf",
        ],
        check=True,
        capture_output=True,
    )
    # Wait for the broker to be ready
    for _ in range(40):
        try:
            s = socket.create_connection(("127.0.0.1", port), timeout=0.25)
            s.close()
            break
        except OSError:
            time.sleep(0.25)
    else:
        subprocess.run(["docker", "rm", "-f", name], check=False, capture_output=True)
        pytest.fail("mosquitto did not come up")
    try:
        yield ("127.0.0.1", port)
    finally:
        subprocess.run(["docker", "rm", "-f", name], check=False, capture_output=True)


def test_bridge_publishes_discovery_and_state_against_fixture(
    knowledge_fixture: Path,
    anchor_today: datetime,
    mosquitto,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    host, port = mosquitto

    # Pin "today"
    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return anchor_today.astimezone(tz)

    monkeypatch.setattr("macos_bridge.time_utils.datetime", FrozenDatetime)

    # Subscribe a recorder client BEFORE the bridge publishes. Subscribe from
    # the on_connect callback so the SUBSCRIBE is sent after CONNACK — issuing
    # it before the network loop / connack would have the broker drop it.
    received: dict[str, str] = {}
    sub = paho.Client(paho.CallbackAPIVersion.VERSION2, client_id="recorder")
    sub.on_message = lambda c, u, msg: received.update({msg.topic: msg.payload.decode()})
    sub.on_connect = lambda c, u, f, rc, p=None: c.subscribe(
        [("homeassistant/#", 1), ("macos/#", 0)]
    )
    sub.connect(host, port, keepalive=15)
    sub.loop_start()
    time.sleep(0.5)

    # Build a non-TLS MQTT config pointing at the embedded broker
    cfg = MqttConfig(
        host=host,
        port=port,
        tls=False,
        username_env="X",
        password_env="Y",  # not used since auth is off
        client_id="bridge-test",
        lwt_topic="macos/test_host/status",
    )
    # MqttPublisher resolves credentials from the env vars named by
    # cfg.username_env / password_env; with auth off on the embedded broker
    # those stay unset (None) and the connection is anonymous.
    pub = MqttPublisher(cfg=cfg, host_slug="test_host", sw_version="0.1.0")
    pub.start()
    assert pub.wait_until_connected(timeout=10.0), "publisher never connected to broker"

    ticker = AggregatesTicker(
        enabled=True,
        interval_seconds=900,
        knowledge_db_path=knowledge_fixture,
        tmp_dir=tmp_path / "work",
        host_slug="test_host",
        topic_prefix="macos",
        availability_topic="macos/test_host/status",
        local_timezone=ZoneInfo("UTC"),
    )
    asyncio.run(ticker.publish_discovery(pub))
    asyncio.run(ticker.run_once(pub))
    time.sleep(1.0)
    pub.stop()
    sub.loop_stop()
    sub.disconnect()

    expected_discovery = {
        f"homeassistant/sensor/macos_test_host_today_{s}/config"
        for s in ("total", "pickups", "top_app", "top_app_minutes")
    }
    assert expected_discovery <= set(received), (
        f"missing discovery topics: {expected_discovery - set(received)}"
    )
    # Values are in hours (deterministic from the synthetic knowledge fixture:
    # 110 min total → 1.83 h, top app 45 min → 0.75 h).
    assert received["macos/test_host/today/total"] == "1.83"
    assert received["macos/test_host/today/pickups"] == "7"
    assert received["macos/test_host/today/top_app"] == "com.apple.logic10"
    assert received["macos/test_host/today/top_app_minutes"] == "0.75"

    # Assert one discovery payload parses as JSON with expected device block
    cfg_payload = json.loads(
        received["homeassistant/sensor/macos_test_host_today_total/config"]
    )
    assert cfg_payload["device"]["identifiers"] == ["macos-mqtt-bridge:test_host"]
