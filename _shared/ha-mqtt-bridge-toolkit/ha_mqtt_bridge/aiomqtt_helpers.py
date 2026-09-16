"""Helpers for asyncio bridges built on aiomqtt.

Bridges split on event-loop model: an asyncio-native bridge (one event
loop coordinating its pollers and the MQTT publisher) uses ``aiomqtt``
instead of the threaded ``ThreadedPublisher`` the other bridges share.

This module exposes the small subset of aiomqtt boilerplate that's
actually generic — primarily the construction-kwargs dict and a sane
default ``ssl.SSLContext`` for TLS deployments. Bridges call
``aiomqtt.Client(**mqtt_client_kwargs(hostname=..., port=...))`` and
manage their own session lifecycle.

The reconnect-with-backoff pattern (``async with aiomqtt.Client(...)``
inside a ``while True`` loop with exponential backoff between failed
connects) is intentionally NOT abstracted here — it's tied to the
caller's task supervision, cancellation semantics, and what to do on
non-recoverable errors. Each bridge writes its own loop.
"""

from __future__ import annotations

import ssl
from typing import Any


def mqtt_client_kwargs(
    *,
    hostname: str,
    port: int = 1883,
    username: str | None = None,
    password: str | None = None,
    tls: bool = False,
    tls_context: ssl.SSLContext | None = None,
    **extra: Any,
) -> dict[str, Any]:
    """Return the kwargs to pass to ``aiomqtt.Client(...)``.

    The bridges' four-or-more construction sites (publisher session
    loop, command listener, log shipper, etc.) all funnel through this
    helper so auth + transport stay in lockstep.

    Credentials:
        ``None`` and empty string are equivalent — both coerce to
        ``None`` on the wire. aiomqtt distinguishes empty-string from
        ``None``, and we always pass ``None`` so unauthenticated
        brokers (test fixtures) don't get a literal empty-string
        username sent.

    TLS:
        Pass ``tls=True`` to enable TLS with a default
        ``ssl.create_default_context()`` (system trust store +
        hostname check + ``CERT_REQUIRED``). Pass ``tls_context``
        directly if you need a custom context (custom CA, client
        cert, etc.); a supplied context takes precedence over
        ``tls=True``.

    Extras:
        Anything in ``**extra`` is merged on top of the defaults, so
        callers can pass ``will=``, ``keepalive=``, ``identifier=``,
        ``protocol=``, etc., or override e.g. ``port`` for a test
        fixture. Caller extras always win.
    """
    kwargs: dict[str, Any] = {
        "hostname": hostname,
        "port": port,
        "username": username or None,
        "password": password or None,
    }
    if tls_context is not None:
        kwargs["tls_context"] = tls_context
    elif tls:
        kwargs["tls_context"] = ssl.create_default_context()
    kwargs.update(extra)
    return kwargs
