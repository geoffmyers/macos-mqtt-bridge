"""Hostname helpers — slug for HA unique_ids, friendly name for display."""

from __future__ import annotations

import logging
import re
import socket
import subprocess

logger = logging.getLogger(__name__)


def slugify_hostname(name: str) -> str:
    """Lowercase, replace separators (hyphens, dots, whitespace) with ``_``,
    strip all other non-alphanumeric characters, then collapse consecutive
    underscores and strip leading/trailing underscores.
    Falls back to 'unknown_host' if the result is empty.
    """
    s = name.lower()
    s = re.sub(r"[-.\s]+", "_", s)
    s = re.sub(r"[^a-z0-9_]", "", s)
    s = re.sub(r"_+", "_", s)
    s = s.strip("_")
    return s or "unknown_host"


def system_hostname_slug() -> str:
    return slugify_hostname(socket.gethostname().split(".")[0])


def system_friendly_hostname() -> str:
    """The macOS user-set Computer Name (e.g. "Alex's MacBook Pro").
    Falls back to the slugified short hostname on non-macOS hosts (CI).
    """
    try:
        result = subprocess.run(
            ["scutil", "--get", "ComputerName"],
            capture_output=True, text=True, timeout=2.0, check=False,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
        logger.warning("scutil failed: %s", exc)
        return system_hostname_slug()
    name = result.stdout.strip()
    return name or system_hostname_slug()


def system_serial_number() -> str | None:
    try:
        result = subprocess.run(
            ["ioreg", "-rd1", "-c", "IOPlatformExpertDevice"],
            capture_output=True, text=True, timeout=2.0, check=False,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
        logger.warning("ioreg failed: %s", exc)
        return None
    if result.returncode != 0:
        return None
    match = re.search(r'"IOPlatformSerialNumber"\s*=\s*"([^"]+)"', result.stdout)
    return match.group(1) if match else None


def system_interface_mac(interface: str = "en0") -> str | None:
    """MAC address of the named hardware interface, lower-cased.
    en0 on Apple Silicon is the built-in Wi-Fi adapter (MAC is permanent).
    """
    try:
        result = subprocess.run(
            ["networksetup", "-listallhardwareports"],
            capture_output=True, text=True, timeout=2.0, check=False,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
        logger.warning("networksetup failed: %s", exc)
        return None
    if result.returncode != 0:
        return None
    pattern = (
        rf"Device:\s*{re.escape(interface)}\s*\nEthernet Address:\s*([0-9a-fA-F:]+)"
    )
    match = re.search(pattern, result.stdout)
    if not match:
        return None
    mac = match.group(1).strip().lower()
    return mac if mac and mac != "n/a" else None
