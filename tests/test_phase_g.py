"""Tests for Phase G host-metrics ticker."""

from __future__ import annotations

import asyncio
import os
import subprocess
from unittest.mock import MagicMock

import pytest

from macos_bridge.phases.host_metrics import HostMetricsTicker


_IOSTAT_OUT = """              disk0       cpu    load average
    KB/t  tps  MB/s  us sy id   1m   5m   15m
   23.54 3773 86.72  10  5 85  1.50  2.00  2.50
   18.45 8739 157.49 20 10 70  1.50  2.00  2.50
"""

_VM_STAT_OUT = """Mach Virtual Memory Statistics: (page size of 16384 bytes)
Pages free:                                     1000.
Pages active:                                  10000.
Pages inactive:                                 5000.
Pages speculative:                                500.
Pages throttled:                                   0.
Pages wired down:                               2000.
Pages purgeable:                                 100.
Pages occupied by compressor:                   3000.
"""

# 16 GB total memory (in bytes)
_HW_MEMSIZE_OUT = "17179869184\n"

# Sat Apr 26 09:43:15 2026 UTC
_BOOTTIME_OUT = "{ sec = 1776923508, usec = 0 } Sat Apr 26 09:43:15 2026\n"


@pytest.fixture
def fake_mqtt() -> MagicMock:
    m = MagicMock()
    m.host_slug = "test_host"
    m.sw_version = "0.1.0"
    m.host_friendly_name = "Test Host"
    return m


def _ticker(disk_mount: str = "/") -> HostMetricsTicker:
    return HostMetricsTicker(
        enabled=True,
        interval_seconds=30,
        host_slug="test_host",
        topic_prefix="macos",
        availability_topic="macos/test_host/status",
        disk_mount=disk_mount,
    )


def _stub(monkeypatch, **outputs):
    """Stub subprocess.run with a per-binary output map."""

    def fake_run(args, *a, **kw):
        first = args[0]
        # sysctl differentiates by second arg
        if first == "sysctl":
            key = args[2] if len(args) > 2 else ""
            if "memsize" in key:
                rc, stdout = outputs.get("__sysctl_memsize", (0, _HW_MEMSIZE_OUT))
            elif "boottime" in key:
                rc, stdout = outputs.get("__sysctl_boottime", (0, _BOOTTIME_OUT))
            else:
                rc, stdout = 0, ""
            return subprocess.CompletedProcess(args, rc, stdout=stdout, stderr="")
        if first in outputs:
            rc, stdout = outputs[first]
            return subprocess.CompletedProcess(args, rc, stdout=stdout, stderr="")
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)


def test_phase_g_publishes_cpu_and_attrs(monkeypatch, fake_mqtt: MagicMock):
    _stub(monkeypatch, iostat=(0, _IOSTAT_OUT), vm_stat=(0, _VM_STAT_OUT))
    monkeypatch.setattr(os, "getloadavg", lambda: (1.5, 2.0, 2.5))

    ticker = _ticker()
    asyncio.run(ticker.run_once(fake_mqtt))

    states = {c.args[0]: c.args[1] for c in fake_mqtt.publish_state.call_args_list}
    attrs = {c.args[0]: c.args[1] for c in fake_mqtt.publish_attributes.call_args_list}

    base = "macos/test_host/host"
    # Live sample is the second iostat line: us=20, sy=10, id=70
    assert states[f"{base}/cpu_percent"] == 30.0  # user + sys
    cpu_attrs = attrs[f"{base}/cpu_percent/attrs"]
    assert cpu_attrs["user_percent"] == 20.0
    assert cpu_attrs["sys_percent"] == 10.0
    assert cpu_attrs["idle_percent"] == 70.0


def test_phase_g_publishes_memory(monkeypatch, fake_mqtt: MagicMock):
    _stub(monkeypatch, iostat=(0, _IOSTAT_OUT), vm_stat=(0, _VM_STAT_OUT))
    monkeypatch.setattr(os, "getloadavg", lambda: (1.5, 2.0, 2.5))

    ticker = _ticker()
    asyncio.run(ticker.run_once(fake_mqtt))

    states = {c.args[0]: c.args[1] for c in fake_mqtt.publish_state.call_args_list}
    base = "macos/test_host/host"

    # used = (active=10000 + wired=2000 + compressor=3000) * 16384 bytes
    #      = 15000 * 16384 = 245760000 bytes ≈ 0.23 GB
    expected_used_gb = round((15000 * 16384) / (1024 ** 3), 2)
    assert states[f"{base}/memory_used_gb"] == expected_used_gb
    # 16 GB total → percent ≈ 1.4
    assert 0 < states[f"{base}/memory_percent"] < 5


def test_phase_g_publishes_disk(monkeypatch, fake_mqtt: MagicMock, tmp_path):
    _stub(monkeypatch, iostat=(0, _IOSTAT_OUT), vm_stat=(0, _VM_STAT_OUT))
    monkeypatch.setattr(os, "getloadavg", lambda: (1.5, 2.0, 2.5))

    ticker = _ticker(disk_mount=str(tmp_path))  # any real path works for statvfs
    asyncio.run(ticker.run_once(fake_mqtt))

    states = {c.args[0]: c.args[1] for c in fake_mqtt.publish_state.call_args_list}
    attrs = {c.args[0]: c.args[1] for c in fake_mqtt.publish_attributes.call_args_list}
    base = "macos/test_host/host"

    assert isinstance(states[f"{base}/disk_used_gb"], float)
    assert states[f"{base}/disk_used_gb"] >= 0
    assert 0 <= states[f"{base}/disk_percent"] <= 100
    assert attrs[f"{base}/disk_used_gb/attrs"]["mount"] == str(tmp_path)


def test_phase_g_publishes_load_average(monkeypatch, fake_mqtt: MagicMock):
    _stub(monkeypatch, iostat=(0, _IOSTAT_OUT), vm_stat=(0, _VM_STAT_OUT))
    monkeypatch.setattr(os, "getloadavg", lambda: (1.5, 2.0, 2.5))

    ticker = _ticker()
    asyncio.run(ticker.run_once(fake_mqtt))

    states = {c.args[0]: c.args[1] for c in fake_mqtt.publish_state.call_args_list}
    base = "macos/test_host/host"
    assert states[f"{base}/load_1m"] == 1.5
    assert states[f"{base}/load_5m"] == 2.0
    assert states[f"{base}/load_15m"] == 2.5


def test_phase_g_publishes_boot_time(monkeypatch, fake_mqtt: MagicMock):
    _stub(monkeypatch, iostat=(0, _IOSTAT_OUT), vm_stat=(0, _VM_STAT_OUT))
    monkeypatch.setattr(os, "getloadavg", lambda: (1.5, 2.0, 2.5))

    ticker = _ticker()
    asyncio.run(ticker.run_once(fake_mqtt))

    states = {c.args[0]: c.args[1] for c in fake_mqtt.publish_state.call_args_list}
    base = "macos/test_host/host"
    # epoch 1776923508 in UTC
    assert states[f"{base}/boot_time"] == "2026-04-23T05:51:48+00:00"


def test_phase_g_disabled_skips_supervisor(monkeypatch, fake_mqtt: MagicMock):
    ticker = HostMetricsTicker(
        enabled=False,
        interval_seconds=30,
        host_slug="test_host",
        topic_prefix="macos",
        availability_topic="macos/test_host/status",
    )
    assert ticker.enabled is False


def test_phase_g_publishes_one_discovery_per_sensor(fake_mqtt: MagicMock):
    ticker = _ticker()
    asyncio.run(ticker.publish_discovery(fake_mqtt))

    # 9 sensors: cpu_percent, memory_used_gb, memory_percent,
    # disk_used_gb, disk_percent, load_1m, load_5m, load_15m, boot_time
    assert fake_mqtt.publish_discovery.call_count == 9
    unique_ids = {c.kwargs["unique_id"] for c in fake_mqtt.publish_discovery.call_args_list}
    assert "macos_test_host_host_cpu_percent" in unique_ids
    assert "macos_test_host_host_memory_used_gb" in unique_ids
    assert "macos_test_host_host_disk_percent" in unique_ids
    assert "macos_test_host_host_load_15m" in unique_ids
    assert "macos_test_host_host_boot_time" in unique_ids


def test_phase_g_iostat_failure_skips_cpu(monkeypatch, fake_mqtt: MagicMock):
    """When iostat times out, cpu sensors are skipped but others still publish."""
    _stub(
        monkeypatch,
        iostat=(0, ""),  # empty output → no regex match
        vm_stat=(0, _VM_STAT_OUT),
    )
    monkeypatch.setattr(os, "getloadavg", lambda: (1.5, 2.0, 2.5))

    ticker = _ticker()
    asyncio.run(ticker.run_once(fake_mqtt))

    state_topics = [c.args[0] for c in fake_mqtt.publish_state.call_args_list]
    assert not any(t.endswith("/cpu_percent") for t in state_topics)
    assert any(t.endswith("/load_1m") for t in state_topics)
