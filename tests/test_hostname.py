import subprocess
from unittest.mock import MagicMock

import pytest
from hypothesis import given
from hypothesis import strategies as st

from macos_bridge.hostname import (
    slugify_hostname,
    system_friendly_hostname,
    system_hostname_slug,
    system_interface_mac,
    system_serial_number,
)


def test_slugify_lowercases():
    assert slugify_hostname("Alexs-MacBook-Pro") == "alexs_macbook_pro"


def test_slugify_replaces_dots_and_hyphens():
    assert slugify_hostname("foo.bar-baz") == "foo_bar_baz"


def test_slugify_strips_invalid_chars():
    assert slugify_hostname("a!b@c#d") == "abcd"


def test_slugify_collapses_consecutive_underscores():
    assert slugify_hostname("a---b") == "a_b"


def test_slugify_strips_leading_and_trailing_underscores():
    assert slugify_hostname("---a---") == "a"


def test_slugify_empty_falls_back():
    assert slugify_hostname("") == "unknown_host"


@given(st.text())
def test_slugify_always_valid_unique_id(s: str):
    result = slugify_hostname(s)
    assert result == "" or all(c.islower() or c.isdigit() or c == "_" for c in result)
    assert "__" not in result
    assert not result.startswith("_")
    assert not result.endswith("_")


def test_system_hostname_slug_is_nonempty():
    assert len(system_hostname_slug()) > 0


def test_system_friendly_hostname_returns_scutil_output(monkeypatch: pytest.MonkeyPatch):
    fake_run = MagicMock(
        return_value=subprocess.CompletedProcess(
            args=[], returncode=0, stdout="Alex's MacBook Pro\n", stderr=""
        )
    )
    monkeypatch.setattr(subprocess, "run", fake_run)
    assert system_friendly_hostname() == "Alex's MacBook Pro"


def test_system_friendly_hostname_falls_back_when_scutil_missing(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(subprocess, "run", MagicMock(side_effect=FileNotFoundError("scutil")))
    result = system_friendly_hostname()
    assert isinstance(result, str) and len(result) > 0


def test_system_friendly_hostname_falls_back_when_scutil_blank(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(
        subprocess,
        "run",
        MagicMock(
            return_value=subprocess.CompletedProcess(args=[], returncode=0, stdout="\n", stderr="")
        ),
    )
    result = system_friendly_hostname()
    assert isinstance(result, str) and len(result) > 0


_IOREG_OUT = """+-o Root  <class IORegistryEntry, id 0x100000100, retain 13>
  | {
  |   "IOPlatformSerialNumber" = "EXAMPLE0001"
  |   "IOPlatformUUID" = "00000000-0000-0000-0000-000000000000"
  | }
"""

_NETWORKSETUP_OUT = """
Hardware Port: Wi-Fi
Device: en0
Ethernet Address: 00:00:5e:00:53:00

Hardware Port: Thunderbolt 1
Device: en1
Ethernet Address: 00:00:5e:00:53:01

Hardware Port: Ethernet Adapter (en4)
Device: en4
Ethernet Address: 00:00:5e:00:53:02

VLAN Configurations
"""


def test_system_serial_number_extracts_from_ioreg(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        subprocess,
        "run",
        MagicMock(
            return_value=subprocess.CompletedProcess(
                args=[], returncode=0, stdout=_IOREG_OUT, stderr=""
            )
        ),
    )
    assert system_serial_number() == "EXAMPLE0001"


def test_system_serial_number_returns_none_when_missing(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        subprocess,
        "run",
        MagicMock(side_effect=FileNotFoundError("ioreg")),
    )
    assert system_serial_number() is None


def test_system_interface_mac_finds_en0(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        subprocess,
        "run",
        MagicMock(
            return_value=subprocess.CompletedProcess(
                args=[], returncode=0, stdout=_NETWORKSETUP_OUT, stderr=""
            )
        ),
    )
    # en0 (Wi-Fi) — the built-in adapter on Apple Silicon
    assert system_interface_mac("en0") == "00:00:5e:00:53:00"
    # en1 (Thunderbolt) — proves we match the right block, not the first one
    assert system_interface_mac("en1") == "00:00:5e:00:53:01"


def test_system_interface_mac_returns_none_for_missing_interface(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(
        subprocess,
        "run",
        MagicMock(
            return_value=subprocess.CompletedProcess(
                args=[], returncode=0, stdout=_NETWORKSETUP_OUT, stderr=""
            )
        ),
    )
    assert system_interface_mac("en99") is None


def test_system_interface_mac_returns_none_when_networksetup_unavailable(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(
        subprocess,
        "run",
        MagicMock(side_effect=FileNotFoundError("networksetup")),
    )
    assert system_interface_mac("en0") is None
