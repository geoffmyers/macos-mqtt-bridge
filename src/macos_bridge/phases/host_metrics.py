"""Phase G — host metrics (CPU, memory, disk, load, uptime).

Polls every `interval_seconds` (default 30s) and publishes:

  sensor.<host>_cpu_percent           — 0-100 (user + sys; user/sys/idle as attrs)
  sensor.<host>_memory_used_gb        — used memory in GB (total/free as attrs)
  sensor.<host>_memory_percent        — used / total * 100
  sensor.<host>_disk_used_gb          — used disk in GB (total/free/mount as attrs)
  sensor.<host>_disk_percent          — used / total * 100
  sensor.<host>_load_1m / _5m / _15m  — system load averages
  sensor.<host>_boot_time             — ISO timestamp of last boot

All readings use stdlib + macOS-builtin subprocesses (iostat, vm_stat, sysctl).
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
from datetime import UTC, datetime

from macos_bridge.mqtt import MqttPublisher, build_discovery_payload
from macos_bridge.phases.base import (
    AbstractTicker,
    screen_time_state_topic,
    screen_time_unique_id,
)

logger = logging.getLogger(__name__)


_BYTES_PER_GB = 1024 ** 3

_IOSTAT_CPU_RE = re.compile(
    r"^\s*(?:[\d.]+\s+){3}(\d+)\s+(\d+)\s+(\d+)\s+", re.MULTILINE
)
_VM_STAT_PAGESIZE_RE = re.compile(r"page size of (\d+) bytes")
_VM_STAT_LINE_RE = re.compile(r"^([^:]+):\s+(\d+)\.?\s*$", re.MULTILINE)
_BOOTTIME_SEC_RE = re.compile(r"sec\s*=\s*(\d+)")


def _run(args: list[str], timeout: float = 5.0) -> str | None:
    try:
        result = subprocess.run(
            args, capture_output=True, text=True, timeout=timeout, check=False
        )
    except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
        logger.debug("%s failed: %s", args[0], exc)
        return None
    return result.stdout if result.returncode == 0 else None


def _cpu_percent() -> dict[str, float | None]:
    """Return {used, user, sys, idle} percentages from `iostat -c 2 -w 1`.

    The first sample is a since-boot average; the second is the live
    1-second sample, so we read the LAST `us sy id` triplet.
    """
    out = _run(["iostat", "-c", "2", "-w", "1"], timeout=4.0)
    if out is None:
        return {"used": None, "user": None, "sys": None, "idle": None}
    matches = _IOSTAT_CPU_RE.findall(out)
    if not matches:
        return {"used": None, "user": None, "sys": None, "idle": None}
    user, sys_, idle = (int(x) for x in matches[-1])
    return {
        "used": float(user + sys_),
        "user": float(user),
        "sys": float(sys_),
        "idle": float(idle),
    }


def _memory() -> dict[str, float | None]:
    total_out = _run(["sysctl", "-n", "hw.memsize"], timeout=2.0)
    vm_out = _run(["vm_stat"], timeout=2.0)
    if total_out is None or vm_out is None:
        return {"used_gb": None, "total_gb": None, "free_gb": None, "percent": None}
    try:
        total_bytes = int(total_out.strip())
    except ValueError:
        return {"used_gb": None, "total_gb": None, "free_gb": None, "percent": None}

    page_match = _VM_STAT_PAGESIZE_RE.search(vm_out)
    page_size = int(page_match.group(1)) if page_match else 16384
    counts: dict[str, int] = {}
    for m in _VM_STAT_LINE_RE.finditer(vm_out):
        key = m.group(1).strip().lower()
        try:
            counts[key] = int(m.group(2))
        except ValueError:
            continue

    # Activity Monitor's "Memory Used" ≈ active + wired_down + compressor
    active = counts.get("pages active", 0)
    wired = counts.get("pages wired down", 0)
    compressed = counts.get("pages occupied by compressor", 0)
    free = counts.get("pages free", 0) + counts.get("pages speculative", 0)

    used_bytes = (active + wired + compressed) * page_size
    free_bytes = free * page_size

    used_gb = round(used_bytes / _BYTES_PER_GB, 2)
    total_gb = round(total_bytes / _BYTES_PER_GB, 2)
    free_gb = round(free_bytes / _BYTES_PER_GB, 2)
    percent = round(used_bytes / total_bytes * 100.0, 1) if total_bytes else None
    return {
        "used_gb": used_gb,
        "total_gb": total_gb,
        "free_gb": free_gb,
        "percent": percent,
    }


def _disk(mount: str) -> dict:
    try:
        s = os.statvfs(mount)
    except OSError as exc:
        logger.warning("statvfs(%s) failed: %s", mount, exc)
        return {
            "used_gb": None,
            "total_gb": None,
            "free_gb": None,
            "percent": None,
            "mount": mount,
        }
    total_bytes = s.f_blocks * s.f_frsize
    free_bytes = s.f_bavail * s.f_frsize
    used_bytes = total_bytes - free_bytes
    total_gb = round(total_bytes / _BYTES_PER_GB, 2)
    free_gb = round(free_bytes / _BYTES_PER_GB, 2)
    used_gb = round(used_bytes / _BYTES_PER_GB, 2)
    percent = round(used_bytes / total_bytes * 100.0, 1) if total_bytes else None
    return {
        "used_gb": used_gb,
        "total_gb": total_gb,
        "free_gb": free_gb,
        "percent": percent,
        "mount": mount,
    }


def _load_average() -> tuple[float, float, float] | None:
    try:
        return os.getloadavg()
    except OSError as exc:
        logger.warning("getloadavg() failed: %s", exc)
        return None


def _boot_time() -> str | None:
    out = _run(["sysctl", "-n", "kern.boottime"], timeout=2.0)
    if out is None:
        return None
    match = _BOOTTIME_SEC_RE.search(out)
    if not match:
        return None
    try:
        epoch = int(match.group(1))
    except ValueError:
        return None
    return datetime.fromtimestamp(epoch, tz=UTC).isoformat()


class HostMetricsTicker(AbstractTicker):
    name = "host_metrics"

    def __init__(
        self,
        *,
        enabled: bool,
        interval_seconds: int,
        host_slug: str,
        topic_prefix: str,
        availability_topic: str,
        disk_mount: str = "/System/Volumes/Data",
    ) -> None:
        self._enabled = enabled
        self._interval_seconds = interval_seconds
        self._host_slug = host_slug
        self._topic_prefix = topic_prefix
        self._availability = availability_topic
        self._disk_mount = disk_mount

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def interval_seconds(self) -> int | None:
        return self._interval_seconds

    def _state_topic(self, suffix: str) -> str:
        return screen_time_state_topic(
            self._topic_prefix, self._host_slug, f"host/{suffix}"
        )

    def _unique_id(self, suffix: str) -> str:
        return screen_time_unique_id(self._host_slug, "host", suffix)

    async def run_once(self, mqtt: MqttPublisher) -> None:
        cpu = _cpu_percent()
        mem = _memory()
        disk = _disk(self._disk_mount)
        load = _load_average()
        boot = _boot_time()

        if cpu["used"] is not None:
            mqtt.publish_state(self._state_topic("cpu_percent"), cpu["used"])
            mqtt.publish_attributes(
                self._state_topic("cpu_percent") + "/attrs",
                {
                    "user_percent": cpu["user"],
                    "sys_percent": cpu["sys"],
                    "idle_percent": cpu["idle"],
                },
            )

        if mem["used_gb"] is not None:
            mem_attrs = {"total_gb": mem["total_gb"], "free_gb": mem["free_gb"]}
            mqtt.publish_state(self._state_topic("memory_used_gb"), mem["used_gb"])
            mqtt.publish_attributes(self._state_topic("memory_used_gb") + "/attrs", mem_attrs)
            mqtt.publish_state(self._state_topic("memory_percent"), mem["percent"])
            mqtt.publish_attributes(self._state_topic("memory_percent") + "/attrs", mem_attrs)

        if disk["used_gb"] is not None:
            disk_attrs = {
                "total_gb": disk["total_gb"],
                "free_gb": disk["free_gb"],
                "mount": disk["mount"],
            }
            mqtt.publish_state(self._state_topic("disk_used_gb"), disk["used_gb"])
            mqtt.publish_attributes(self._state_topic("disk_used_gb") + "/attrs", disk_attrs)
            mqtt.publish_state(self._state_topic("disk_percent"), disk["percent"])
            mqtt.publish_attributes(self._state_topic("disk_percent") + "/attrs", disk_attrs)

        if load is not None:
            load_1m, load_5m, load_15m = load
            mqtt.publish_state(self._state_topic("load_1m"), round(load_1m, 2))
            mqtt.publish_state(self._state_topic("load_5m"), round(load_5m, 2))
            mqtt.publish_state(self._state_topic("load_15m"), round(load_15m, 2))

        if boot is not None:
            mqtt.publish_state(self._state_topic("boot_time"), boot)

        logger.debug(
            "host_metrics: cpu=%s%% mem=%sGB (%s%%) disk=%sGB (%s%%) load=%s boot=%s",
            cpu["used"], mem["used_gb"], mem["percent"],
            disk["used_gb"], disk["percent"], load, boot,
        )

    async def publish_discovery(self, mqtt: MqttPublisher) -> None:
        device = mqtt.host_device_block()

        sensors: list[tuple[str, str, str | None, str | None, str | None, str | None, bool]] = [
            ("cpu_percent", "System - CPU Usage", None, "%", "mdi:cpu-64-bit", "measurement", True),
            ("memory_used_gb", "System - Memory Used", "data_size", "GB",
             "mdi:memory", "measurement", True),
            ("memory_percent", "System - Memory Usage", None, "%", "mdi:memory", "measurement", True),
            ("disk_used_gb", "System - Disk Used", "data_size", "GB", "mdi:harddisk", "measurement", True),
            ("disk_percent", "System - Disk Usage", None, "%", "mdi:harddisk", "measurement", True),
            ("load_1m", "System - Load Average 1m", None, None, "mdi:gauge", "measurement", False),
            ("load_5m", "System - Load Average 5m", None, None, "mdi:gauge", "measurement", False),
            ("load_15m", "System - Load Average 15m", None, None, "mdi:gauge", "measurement", False),
            ("boot_time", "System - Last Boot", "timestamp", None, "mdi:restart", None, False),
        ]
        for suffix, name, device_class, unit, icon, state_class, has_attrs in sensors:
            # Host metrics (CPU / memory / disk / load / boot time) are
            # system-info, not user-facing. HA's "diagnostic" entity
            # category hides them from the main device card and tucks
            # them under the device's diagnostic section instead.
            mqtt.publish_discovery(
                component="sensor",
                unique_id=self._unique_id(suffix),
                payload=build_discovery_payload(
                    name=name,
                    unique_id=self._unique_id(suffix),
                    state_topic=self._state_topic(suffix),
                    availability_topic=self._availability,
                    device=device,
                    device_class=device_class,
                    unit_of_measurement=unit,
                    state_class=state_class,
                    icon=icon,
                    entity_category="diagnostic",
                    json_attributes_topic=(
                        self._state_topic(suffix) + "/attrs" if has_attrs else None
                    ),
                ),
            )
