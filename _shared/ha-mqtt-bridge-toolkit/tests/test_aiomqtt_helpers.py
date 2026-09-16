"""Tests for ha_mqtt_bridge.aiomqtt_helpers.

The helper is small and stateless — these tests pin the four
contractual behaviors asyncio bridges depend on:

  - Empty-string credentials coerce to ``None`` (aiomqtt is sensitive
    to this; an empty username string gets sent on the wire and the
    broker treats it as authenticated).
  - ``tls=True`` produces a default SSLContext with hostname check and
    CERT_REQUIRED (the LE-fronted broker config).
  - A supplied ``tls_context`` wins over ``tls=True``.
  - ``**extra`` kwargs merge on top of defaults, with caller values
    winning so a test fixture can override e.g. ``port``.
"""

from __future__ import annotations

import ssl

from ha_mqtt_bridge.aiomqtt_helpers import mqtt_client_kwargs


class TestPlaintext:
    def test_basic(self) -> None:
        kw = mqtt_client_kwargs(hostname="broker", port=1883)
        assert kw["hostname"] == "broker"
        assert kw["port"] == 1883
        assert kw["username"] is None
        assert kw["password"] is None
        assert "tls_context" not in kw

    def test_with_credentials(self) -> None:
        kw = mqtt_client_kwargs(
            hostname="broker", username="u", password="p"
        )
        assert kw["username"] == "u"
        assert kw["password"] == "p"

    def test_empty_string_credentials_coerce_to_none(self) -> None:
        # aiomqtt distinguishes empty-string from None on the wire.
        kw = mqtt_client_kwargs(hostname="broker", username="", password="")
        assert kw["username"] is None
        assert kw["password"] is None

    def test_default_port(self) -> None:
        kw = mqtt_client_kwargs(hostname="broker")
        assert kw["port"] == 1883


class TestTls:
    def test_tls_true_uses_default_context(self) -> None:
        kw = mqtt_client_kwargs(hostname="mqtt.example.com", port=8883, tls=True)
        ctx = kw["tls_context"]
        assert isinstance(ctx, ssl.SSLContext)
        # Default context behavior: hostname check on, peer cert required.
        assert ctx.check_hostname is True
        assert ctx.verify_mode == ssl.CERT_REQUIRED

    def test_tls_false_no_context(self) -> None:
        kw = mqtt_client_kwargs(hostname="b", tls=False)
        assert "tls_context" not in kw

    def test_custom_context_wins_over_tls_true(self) -> None:
        custom = ssl.create_default_context()
        custom.check_hostname = False  # marker so we can tell it's "ours"
        custom.verify_mode = ssl.CERT_NONE
        kw = mqtt_client_kwargs(hostname="b", tls=True, tls_context=custom)
        assert kw["tls_context"] is custom

    def test_custom_context_without_tls_true(self) -> None:
        custom = ssl.create_default_context()
        kw = mqtt_client_kwargs(hostname="b", tls=False, tls_context=custom)
        assert kw["tls_context"] is custom


class TestExtras:
    def test_extras_merge(self) -> None:
        sentinel_will = object()
        kw = mqtt_client_kwargs(
            hostname="b", keepalive=30, will=sentinel_will
        )
        assert kw["keepalive"] == 30
        assert kw["will"] is sentinel_will

    def test_extras_can_override_named_defaults(self) -> None:
        # Named kwargs like `port` go into the returned dict directly,
        # but if a caller explicitly passes one via `**extra` (e.g.
        # `**custom_overrides`) it lands in the same key and wins.
        # The test calls the helper once with the named arg and a
        # second time injecting `port` via `**extra` — both shapes
        # produce the expected port.
        kw = mqtt_client_kwargs(hostname="b", port=1234)
        assert kw["port"] == 1234

        extras = {"port": 9999}
        kw2 = mqtt_client_kwargs(hostname="b", **extras)
        assert kw2["port"] == 9999

    def test_extra_kwargs_passthrough(self) -> None:
        # Any unknown kwarg (e.g. aiomqtt's `identifier`, `clean_start`,
        # `transport`) is included in the result so the caller can pass
        # them straight to ``aiomqtt.Client(**kwargs)``.
        kw = mqtt_client_kwargs(
            hostname="b",
            identifier="my-client",
            clean_start=True,
        )
        assert kw["identifier"] == "my-client"
        assert kw["clean_start"] is True
