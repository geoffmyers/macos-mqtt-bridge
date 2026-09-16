"""Tests for ha_mqtt_bridge.topics."""

from __future__ import annotations

import pytest

from ha_mqtt_bridge.topics import (
    discovery_topic,
    event_topic,
    slugify,
    slugify_hostname,
    state_topic,
)


class TestSlugify:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("Hello World", "hello_world"),
            ("Alex's iMac (Pro)", "alex_s_imac_pro"),
            ("foo___bar", "foo_bar"),
            ("ALREADY_OK", "already_ok"),
            ("  leading and trailing  ", "leading_and_trailing"),
            ("123abc", "123abc"),
            ("with-dashes-and.dots", "with_dashes_and_dots"),
            ("", ""),
            ("!!!", ""),
        ],
    )
    def test_cases(self, raw: str, expected: str) -> None:
        assert slugify(raw) == expected

    def test_idempotent(self) -> None:
        for s in ("foo bar", "X-1", "Alex's iMac"):
            assert slugify(slugify(s)) == slugify(s)


class TestSlugifyHostname:
    def test_strips_dns_suffix(self) -> None:
        assert slugify_hostname("mac.local") == "mac"

    def test_lowercases(self) -> None:
        assert slugify_hostname("MyMac") == "mymac"

    def test_explicit_passed(self) -> None:
        # Doesn't fall back to socket.gethostname() when arg supplied.
        assert slugify_hostname("Office-Mini.lan") == "office_mini"


class TestStateTopic:
    def test_three_segments(self) -> None:
        assert state_topic("macos", "mac1", "state/foo") == "macos/mac1/state/foo"

    def test_no_host(self) -> None:
        assert state_topic("govee/leak", None, "ABCD/leak") == "govee/leak/ABCD/leak"

    def test_empty_host_is_dropped(self) -> None:
        assert state_topic("p", "", "s") == "p/s"

    def test_suffix_with_slashes_passthrough(self) -> None:
        assert state_topic("p", "h", "a/b/c") == "p/h/a/b/c"


class TestDiscoveryTopic:
    def test_basic(self) -> None:
        assert (
            discovery_topic("homeassistant", "sensor", "abc_temp")
            == "homeassistant/sensor/abc_temp/config"
        )

    def test_with_node_id(self) -> None:
        # HA accepts up to one slash in unique_id (node_id/object_id)
        assert (
            discovery_topic("homeassistant", "binary_sensor", "node/leak")
            == "homeassistant/binary_sensor/node/leak/config"
        )


class TestEventTopic:
    def test_multiple_segments(self) -> None:
        assert (
            event_topic("macos", "imac1", "messages", "received")
            == "macos/imac1/messages/received"
        )

    def test_no_host(self) -> None:
        assert event_topic("p", None, "a", "b") == "p/a/b"

    def test_empty_segments_dropped(self) -> None:
        assert event_topic("p", "h", "", "a", "") == "p/h/a"
