"""HTTP retry-with-backoff for the cloud-API polling bridges.

Every cloud-API bridge in this family (Govee, DroneMobile, Emporia Vue,
Flume, Tractive) polls a REST API on a fixed cadence and hits the same
wall when that API pushes back: a 429 (or a transient 5xx) needs a
retry with backoff, not a crash. Two bridges (DroneMobile, Tractive)
each independently wrote a `_get()`/`_request()` helper with exactly
this retry loop in their own `events.py`, and two more call sites
(``main.py``'s own `_request`/`_get`) raised a *bare* `RuntimeError` on
429 with no retry at all — unguarded in the main loop, which killed the
process on the first rate limit during startup discovery. This module
factors the one correct implementation out so every call site (events
stream AND main request helper, across all five bridges) shares it.

Imports ``requests`` lazily so the toolkit's core publisher/discovery
helpers stay dependency-free for bridges that don't poll a cloud REST
API (the BMW, macOS and Find My bridges talk to a car/keychain/local
cache, not a cloud HTTP API).
"""

from __future__ import annotations

import logging
import time
from typing import Any, Callable

log = logging.getLogger(__name__)

DEFAULT_RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})


class RetryExhaustedError(RuntimeError):
    """Raised when `request_with_backoff` gives up after `max_attempts`
    consecutive retryable (429/5xx by default) responses.

    A dedicated type — not a bare `RuntimeError` — so a bridge's main
    loop (or any caller) can tell "the API is persistently
    rate-limiting/erroring, back off and try again next cycle" apart
    from a genuine bug elsewhere raising a plain `RuntimeError`. Still a
    `RuntimeError` subclass so existing `except RuntimeError` call sites
    keep working unchanged.
    """


def request_with_backoff(
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    params: dict[str, Any] | None = None,
    json_body: dict[str, Any] | None = None,
    data: Any = None,
    timeout: float = 20.0,
    max_attempts: int = 4,
    initial_backoff: float = 2.0,
    max_backoff: float = 60.0,
    retry_statuses: frozenset[int] = DEFAULT_RETRY_STATUSES,
    sleep: Callable[[float], None] = time.sleep,
):
    """Issue one HTTP request, retrying with exponential backoff while
    the response status is in ``retry_statuses`` (429 + 5xx by default).

    Returns the first `requests.Response` whose status code is NOT in
    `retry_statuses` — including a 4xx like 401 or 404 — so the caller
    still owns its own status-code handling (401 → `PermissionError`,
    JSON decoding, any bridge-specific "empty body is a steady state"
    logic). This function's only job is the retry loop.

    Raises `RetryExhaustedError` if every one of `max_attempts` attempts
    landed in `retry_statuses`. Never raises on a network-level error
    (connection refused, timeout, DNS failure, ...) — those propagate as
    the underlying `requests.RequestException` on the first attempt,
    same as an unwrapped `requests.request()` call, since a bridge's
    main loop already has to handle that class of failure separately
    (and blindly retrying a DNS failure 4 times inline would just make
    a real outage take 4x as long to report).
    """
    try:
        import requests  # type: ignore[import-not-found]
    except ImportError as e:  # pragma: no cover - exercised via bridges' own deps
        raise ImportError(
            "request_with_backoff requires the `requests` package — "
            "install with `pip install requests` or "
            "`pip install 'ha-mqtt-bridge-toolkit[http]'`."
        ) from e

    backoff = initial_backoff
    last_status: int | None = None
    for attempt in range(1, max_attempts + 1):
        response = requests.request(
            method,
            url,
            headers=headers,
            params=params,
            json=json_body,
            data=data,
            timeout=timeout,
        )
        if response.status_code not in retry_statuses:
            return response
        last_status = response.status_code
        if attempt == max_attempts:
            break
        log.warning(
            "%s %s -> HTTP %d, retrying in %.0fs (attempt %d/%d)",
            method, url, response.status_code, backoff, attempt, max_attempts,
        )
        sleep(backoff)
        backoff = min(backoff * 2, max_backoff)
    raise RetryExhaustedError(
        f"exhausted {max_attempts} attempts on {method} {url}; "
        f"last status={last_status}"
    )
