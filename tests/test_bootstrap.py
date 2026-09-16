"""Tests for the Phase B bootstrap-allowlist helper."""

from __future__ import annotations

import io
from datetime import datetime
from pathlib import Path

import pytest

from macos_bridge.bootstrap import (
    _is_user_app,
    bootstrap_allowlist,
    emit_yaml,
)


def test_is_user_app_accepts_known_prefixes():
    assert _is_user_app("com.apple.Safari")
    assert _is_user_app("com.tinyspeck.slackmacgap")
    assert _is_user_app("com.microsoft.VSCode")


def test_is_user_app_rejects_apple_builtins():
    assert not _is_user_app("com.apple.WindowManager")
    assert not _is_user_app("com.apple.dock")
    assert not _is_user_app("com.apple.finder")


def test_is_user_app_rejects_helpers_and_agents():
    assert not _is_user_app("com.apple.someservice.helper")
    assert not _is_user_app("com.foo.someagent")
    assert not _is_user_app("com.acme.WindowExtension")


def test_is_user_app_rejects_unknown_prefixes():
    assert not _is_user_app("net.example.unknown")
    assert not _is_user_app("")


def test_bootstrap_allowlist_against_fixture(
    knowledge_fixture: Path, anchor_today: datetime, monkeypatch: pytest.MonkeyPatch
):
    """Bootstrap should pick up Safari/Slack/Logic, drop the helper + WindowManager."""

    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return anchor_today.astimezone(tz)

    monkeypatch.setattr("macos_bridge.time_utils.datetime", FrozenDatetime)
    # Avoid mdfind I/O
    monkeypatch.setattr(
        "macos_bridge.bootstrap.bundle_id_to_app_name",
        lambda bid: bid.split(".")[-1].title() if bid else bid,
    )

    apps = bootstrap_allowlist(
        knowledge_fixture, knowledge_fixture.parent / "work", top_n=20, days=1
    )
    bundle_ids = [a[0] for a in apps]
    assert "com.apple.Safari" in bundle_ids
    assert "com.tinyspeck.slackmacgap" in bundle_ids
    assert "com.apple.logic10" in bundle_ids
    assert "com.apple.WindowManager" not in bundle_ids
    assert "com.apple.someservice.helper" not in bundle_ids


def test_emit_yaml_format():
    apps = [
        ("com.apple.Safari", 30, "Safari"),
        ("com.apple.logic10", 45, "Logic Pro"),
    ]
    buf = io.StringIO()
    emit_yaml(apps, stream=buf)
    out = buf.getvalue()
    assert "per_app:" in out
    assert "apps:" in out
    assert "bundle_id: com.apple.Safari" in out
    assert "friendly_name: Safari" in out
    assert "30m last 30d" in out
