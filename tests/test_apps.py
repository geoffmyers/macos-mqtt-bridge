"""Tests for apps.bundle_id_to_app_name."""

from __future__ import annotations

import subprocess
from unittest.mock import MagicMock

import pytest

from macos_bridge import apps


def test_friendly_overrides_win_over_mdfind(monkeypatch: pytest.MonkeyPatch):
    """Hand-curated overrides should bypass mdfind entirely so apps whose
    .app filename is a code-style token (bzbmenu, etc.) display as their
    user-recognized name."""
    apps.bundle_id_to_app_name.cache_clear()
    # mdfind should NOT be invoked at all when an override exists.
    fake_run = MagicMock(side_effect=AssertionError("mdfind should not be called"))
    monkeypatch.setattr(subprocess, "run", fake_run)
    assert apps.bundle_id_to_app_name("com.backblaze.bzbmenu") == "Backblaze Menu"
    assert apps.bundle_id_to_app_name("com.apple.loginwindow") == "Login Window"
    fake_run.assert_not_called()


def test_bundle_id_to_app_name_returns_app_filename(monkeypatch: pytest.MonkeyPatch):
    apps.bundle_id_to_app_name.cache_clear()
    fake_run = MagicMock(
        return_value=subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout="/Applications/Visual Studio Code.app\n",
            stderr="",
        )
    )
    monkeypatch.setattr(subprocess, "run", fake_run)
    assert apps.bundle_id_to_app_name("com.microsoft.VSCode") == "Visual Studio Code"


def test_bundle_id_to_app_name_picks_first_app_match(monkeypatch: pytest.MonkeyPatch):
    apps.bundle_id_to_app_name.cache_clear()
    fake_run = MagicMock(
        return_value=subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout="/Applications/Safari.app\n/Volumes/Time Machine/Applications/Safari.app\n",
            stderr="",
        )
    )
    monkeypatch.setattr(subprocess, "run", fake_run)
    assert apps.bundle_id_to_app_name("com.apple.Safari") == "Safari"


def test_bundle_id_to_app_name_falls_back_to_bundle_id_when_not_found(
    monkeypatch: pytest.MonkeyPatch,
):
    apps.bundle_id_to_app_name.cache_clear()
    fake_run = MagicMock(
        return_value=subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout="",
            stderr="",
        )
    )
    monkeypatch.setattr(subprocess, "run", fake_run)
    assert apps.bundle_id_to_app_name("com.foo.unknown") == "com.foo.unknown"


def test_bundle_id_to_app_name_handles_timeout(monkeypatch: pytest.MonkeyPatch):
    apps.bundle_id_to_app_name.cache_clear()
    monkeypatch.setattr(
        subprocess,
        "run",
        MagicMock(side_effect=subprocess.TimeoutExpired(cmd="mdfind", timeout=2.0)),
    )
    assert apps.bundle_id_to_app_name("com.apple.Safari") == "com.apple.Safari"


def test_transient_timeout_is_not_cached(monkeypatch: pytest.MonkeyPatch):
    """A transient mdfind timeout must not pin the bundle_id as the answer:
    a follow-up successful mdfind call should yield the real .app name.

    Regression: previously @lru_cache memoized the timeout fallback, so VS
    Code permanently displayed as ``com.microsoft.VSCode`` after a single
    Spotlight hiccup until the bridge was restarted.
    """
    apps.bundle_id_to_app_name.cache_clear()
    fake_run = MagicMock(
        side_effect=[
            subprocess.TimeoutExpired(cmd="mdfind", timeout=2.0),
            subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout="/Applications/Visual Studio Code.app\n",
                stderr="",
            ),
        ]
    )
    monkeypatch.setattr(subprocess, "run", fake_run)

    assert apps.bundle_id_to_app_name("com.microsoft.VSCode") == "com.microsoft.VSCode"
    assert apps.bundle_id_to_app_name("com.microsoft.VSCode") == "Visual Studio Code"
    # Subsequent calls hit the cache and never re-invoke mdfind.
    assert apps.bundle_id_to_app_name("com.microsoft.VSCode") == "Visual Studio Code"
    assert fake_run.call_count == 2


def test_bundle_id_to_app_name_empty_input_returns_empty():
    assert apps.bundle_id_to_app_name("") == ""


def test_bundle_id_to_app_name_skips_non_app_paths(monkeypatch: pytest.MonkeyPatch):
    apps.bundle_id_to_app_name.cache_clear()
    fake_run = MagicMock(
        return_value=subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout="/Some/Path/Not-An-App\n/Applications/Real App.app\n",
            stderr="",
        )
    )
    monkeypatch.setattr(subprocess, "run", fake_run)
    assert apps.bundle_id_to_app_name("com.example.App") == "Real App"
