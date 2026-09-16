"""Static macOS / hardware identity diagnostics.

Publishes ~12 sensors that describe what this Mac IS rather than what
it's currently doing — OS version + build, hardware model + serial +
UUID, chip + cores, total memory, storage capacity. All values are
nearly-static for the lifetime of the device (or change only across an
OS upgrade), so the cadence defaults to 1 hour.

Most fields come from cheap ``sysctl`` / ``sw_vers`` / ``scutil`` calls;
only Model Name / Hardware UUID / Serial Number need
``system_profiler SPHardwareDataType`` (~1-3s warm).

All sensors are ``entity_category=diagnostic`` and omit availability —
they're historical-static identity data, not live signals.

Published under the ``system_info/`` topic suffix so they don't collide
with system_state's ``system/`` (live system stats) or host_metrics'
``host/`` (CPU/RAM/disk) trees.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
from typing import Any

from macos_bridge.mqtt import MqttPublisher, build_discovery_payload
from macos_bridge.phases.base import (
    AbstractTicker,
    screen_time_state_topic,
    screen_time_unique_id,
)

logger = logging.getLogger(__name__)

_SP_FIELDS_RE = {
    "model_name": re.compile(r"^\s*Model Name:\s*(.+)$", re.MULTILINE),
    "model_identifier": re.compile(r"^\s*Model Identifier:\s*(.+)$", re.MULTILINE),
    "model_number": re.compile(r"^\s*Model Number:\s*(.+)$", re.MULTILINE),
    "chip": re.compile(r"^\s*Chip:\s*(.+)$", re.MULTILINE),
    "total_cores": re.compile(r"^\s*Total Number of Cores:\s*(.+)$", re.MULTILINE),
    "memory_text": re.compile(r"^\s*Memory:\s*(.+)$", re.MULTILINE),
    "serial_number": re.compile(
        r"^\s*Serial Number\s*\(system\):\s*(.+)$", re.MULTILINE
    ),
    "hardware_uuid": re.compile(r"^\s*Hardware UUID:\s*(.+)$", re.MULTILINE),
    "provisioning_udid": re.compile(r"^\s*Provisioning UDID:\s*(.+)$", re.MULTILINE),
}


def _run(args: list[str], timeout: float = 5.0) -> str:
    try:
        result = subprocess.run(
            args, capture_output=True, text=True, timeout=timeout, check=False
        )
    except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
        logger.debug("%s failed: %s", args[0], exc)
        return ""
    return result.stdout if result.returncode == 0 else ""


def _sysctl(key: str) -> str:
    return _run(["sysctl", "-n", key], timeout=2.0).strip()


def _sw_vers(flag: str) -> str:
    return _run(["sw_vers", flag], timeout=2.0).strip()


def _format_bytes_gb(byte_count: int) -> str:
    """Apple's hardware label for memory uses 1024^3 (GiB) but reports it
    as 'GB'. We follow the same convention so '64 GB' matches what the
    user sees in About This Mac."""
    if byte_count <= 0:
        return ""
    gb = round(byte_count / (1024 ** 3))
    return f"{gb} GB"


def _format_bytes_storage(byte_count: int) -> str:
    """Storage marketing capacity uses base-10 (TB / GB) so a 2 TB SSD
    reports as 1.8 TiB on disk. Show whichever matches the user's
    expectation: TB if the number is round, GB otherwise."""
    if byte_count <= 0:
        return ""
    if byte_count >= 1_000_000_000_000:
        return f"{byte_count / 1_000_000_000_000:.1f} TB"
    return f"{round(byte_count / 1_000_000_000)} GB"


def _system_profiler_hardware(timeout: float = 10.0) -> dict[str, str]:
    """One ``system_profiler SPHardwareDataType`` call → dict of fields.
    Slower than sysctl but the only source for ``Model Name``,
    ``Hardware UUID``, ``Provisioning UDID``, and the user-friendly
    ``Memory`` text."""
    text = _run(["system_profiler", "SPHardwareDataType"], timeout=timeout)
    out: dict[str, str] = {}
    for key, pattern in _SP_FIELDS_RE.items():
        m = pattern.search(text)
        if m:
            out[key] = m.group(1).strip()
    return out


def _root_volume_capacity_bytes() -> int:
    """Total bytes on the user's data volume (the user-visible "disk
    capacity"). Uses ``df`` for portability."""
    text = _run(["df", "-k", "/System/Volumes/Data"], timeout=3.0)
    lines = [ln for ln in text.splitlines() if ln.startswith("/")]
    if not lines:
        return 0
    parts = lines[0].split()
    if len(parts) < 4:
        return 0
    try:
        # df -k: Available + Used = Total. The "1024-blocks" column is
        # the *current* sum, but APFS reports per-volume free space —
        # adding Used (col 2) + Avail (col 3) gives the volume capacity.
        used_kb = int(parts[2])
        avail_kb = int(parts[3])
        return (used_kb + avail_kb) * 1024
    except (ValueError, IndexError):
        return 0


def _computer_name(timeout: float = 2.0) -> str:
    """``scutil --get ComputerName`` returns the user-set Mac name (e.g.
    'Alex's MacBook Pro')."""
    return _run(["scutil", "--get", "ComputerName"], timeout=timeout).strip()


def _collect() -> dict[str, str]:
    """Gather every diagnostic field we publish. Keys map directly to
    sensor suffixes (and to the unique_id slugs)."""
    sp = _system_profiler_hardware()
    mem_bytes = int(_sysctl("hw.memsize") or "0")
    perf_cores = _sysctl("hw.perflevel0.physicalcpu")
    eff_cores = _sysctl("hw.perflevel1.physicalcpu")

    return {
        "macos_version": _sw_vers("-productVersion"),
        "macos_build": _sw_vers("-buildVersion"),
        "macos_product": _sw_vers("-productName"),
        "kernel_version": _sysctl("kern.osrelease"),
        "hardware_name": sp.get("model_name", ""),
        "hardware_model": _sysctl("hw.model") or sp.get("model_identifier", ""),
        "hardware_model_number": sp.get("model_number", ""),
        "serial_number": sp.get("serial_number", ""),
        "hardware_uuid": sp.get("hardware_uuid", ""),
        "computer_name": _computer_name(),
        "chip": sp.get("chip", "") or _sysctl("machdep.cpu.brand_string"),
        "cpu_cores_total": _sysctl("hw.physicalcpu"),
        "cpu_cores_performance": perf_cores,
        "cpu_cores_efficiency": eff_cores,
        "total_memory": (
            sp.get("memory_text", "") or _format_bytes_gb(mem_bytes)
        ),
        "storage_capacity": _format_bytes_storage(_root_volume_capacity_bytes()),
    }


# (suffix, friendly_name, icon)
_SENSORS: list[tuple[str, str, str]] = [
    ("macos_version", "System - macOS Version", "mdi:apple"),
    ("macos_build", "System - macOS Build", "mdi:apple"),
    ("kernel_version", "System - Kernel Version", "mdi:linux"),
    ("hardware_name", "System - Hardware Name", "mdi:laptop"),
    ("hardware_model", "System - Hardware Model", "mdi:identifier"),
    ("serial_number", "System - Serial Number", "mdi:barcode"),
    ("hardware_uuid", "System - Hardware UUID", "mdi:fingerprint"),
    ("computer_name", "System - Computer Name", "mdi:label"),
    ("chip", "System - Chip", "mdi:cpu-64-bit"),
    ("cpu_cores_total", "System - CPU Cores", "mdi:cpu-64-bit"),
    ("total_memory", "System - Total Memory", "mdi:memory"),
    ("storage_capacity", "System - Storage Capacity", "mdi:harddisk"),
]


class SystemInfoTicker(AbstractTicker):
    name = "system_info"

    def __init__(
        self,
        *,
        enabled: bool,
        interval_seconds: int,
        host_slug: str,
        topic_prefix: str,
        availability_topic: str,
    ) -> None:
        self._enabled = enabled
        self._interval_seconds = interval_seconds
        self._host_slug = host_slug
        self._topic_prefix = topic_prefix
        # ``_availability`` is unused (these are static-identity sensors that
        # never need a stale-state indicator) but kept for API symmetry with
        # the other tickers in case a future option re-enables it.
        self._availability = availability_topic

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def interval_seconds(self) -> int | None:
        return self._interval_seconds

    def _state_topic(self, suffix: str) -> str:
        return screen_time_state_topic(
            self._topic_prefix, self._host_slug, f"system_info/{suffix}"
        )

    def _attrs_topic(self) -> str:
        return screen_time_state_topic(
            self._topic_prefix, self._host_slug, "system_info/attrs"
        )

    def _unique_id(self, suffix: str) -> str:
        return screen_time_unique_id(self._host_slug, "system_info", suffix)

    async def run_once(self, mqtt: MqttPublisher) -> None:
        info = _collect()
        for suffix, _name, _icon in _SENSORS:
            value = info.get(suffix, "")
            if value:
                mqtt.publish_state(self._state_topic(suffix), value)

        # Publish the full snapshot as attrs on the chip sensor's adjacent
        # topic so HA templates can read fields that don't have their own
        # entity (perf/efficiency core split, model_number, kernel, etc.).
        attrs: dict[str, Any] = dict(info)
        mqtt.publish_attributes(self._attrs_topic(), attrs)

        logger.info(
            "system_info tick: %s %s (%s) %s / %s cores / %s",
            info.get("hardware_name", "?"),
            info.get("hardware_model", "?"),
            info.get("chip", "?"),
            info.get("total_memory", "?"),
            info.get("cpu_cores_total", "?"),
            info.get("storage_capacity", "?"),
        )

    async def publish_discovery(self, mqtt: MqttPublisher) -> None:
        device = mqtt.host_device_block()

        for suffix, name, icon in _SENSORS:
            uid = self._unique_id(suffix)
            # Attach the rich attrs payload to the Hardware UUID sensor
            # (a stable, always-present field) so HA exposes the full
            # snapshot under one entity's "attributes" view.
            attrs_topic = self._attrs_topic() if suffix == "hardware_uuid" else None
            mqtt.publish_discovery(
                component="sensor",
                unique_id=uid,
                payload=build_discovery_payload(
                    name=name,
                    unique_id=uid,
                    state_topic=self._state_topic(suffix),
                    availability_topic=None,
                    device=device,
                    icon=icon,
                    entity_category="diagnostic",
                    json_attributes_topic=attrs_topic,
                ),
            )
