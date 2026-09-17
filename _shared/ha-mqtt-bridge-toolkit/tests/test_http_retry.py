"""Tests for ha_mqtt_bridge.http_retry.

`requests` isn't mocked at the transport layer here — we monkeypatch
`requests.request` itself, since `request_with_backoff` is a thin loop
around exactly that call.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from ha_mqtt_bridge.http_retry import RetryExhaustedError, request_with_backoff


def _resp(status_code: int) -> MagicMock:
    r = MagicMock()
    r.status_code = status_code
    return r


class TestRequestWithBackoff:
    def test_success_first_attempt_no_sleep(self):
        sleeps = []
        with patch("requests.request", return_value=_resp(200)) as req:
            r = request_with_backoff(
                "GET", "https://example.com/x", sleep=sleeps.append,
            )
        assert r.status_code == 200
        assert req.call_count == 1
        assert sleeps == []

    def test_non_retryable_4xx_returned_immediately(self):
        # 401/404/etc. are the CALLER's problem (auth refresh, steady
        # state, ...) — this function must not swallow or retry them.
        with patch("requests.request", return_value=_resp(404)) as req:
            r = request_with_backoff("GET", "https://example.com/x", sleep=lambda s: None)
        assert r.status_code == 404
        assert req.call_count == 1

    def test_retries_429_then_succeeds(self):
        sleeps = []
        responses = [_resp(429), _resp(429), _resp(200)]
        with patch("requests.request", side_effect=responses) as req:
            r = request_with_backoff(
                "GET", "https://example.com/x",
                sleep=sleeps.append, initial_backoff=1.0,
            )
        assert r.status_code == 200
        assert req.call_count == 3
        # Exponential: 1.0, then 2.0 before the third (successful) attempt.
        assert sleeps == [1.0, 2.0]

    def test_exhausts_retries_raises_dedicated_type(self):
        with patch("requests.request", return_value=_resp(429)) as req:
            with pytest.raises(RetryExhaustedError, match="last status=429"):
                request_with_backoff(
                    "GET", "https://example.com/x",
                    max_attempts=3, sleep=lambda s: None,
                )
        assert req.call_count == 3

    def test_retry_exhausted_is_a_runtime_error(self):
        # Existing call sites written against a bare `except RuntimeError`
        # must keep working after this type replaces it.
        with patch("requests.request", return_value=_resp(503)):
            with pytest.raises(RuntimeError):
                request_with_backoff(
                    "GET", "https://example.com/x",
                    max_attempts=2, sleep=lambda s: None,
                )

    def test_backoff_capped_at_max_backoff(self):
        sleeps = []
        responses = [_resp(500)] * 4 + [_resp(200)]
        with patch("requests.request", side_effect=responses):
            request_with_backoff(
                "GET", "https://example.com/x",
                max_attempts=5, initial_backoff=10.0, max_backoff=15.0,
                sleep=sleeps.append,
            )
        assert sleeps == [10.0, 15.0, 15.0, 15.0]

    def test_custom_retry_statuses(self):
        # A caller that only wants to retry 429 (not 5xx) should be able
        # to narrow the retry set.
        with patch("requests.request", return_value=_resp(500)) as req:
            r = request_with_backoff(
                "GET", "https://example.com/x",
                retry_statuses=frozenset({429}), sleep=lambda s: None,
            )
        assert r.status_code == 500
        assert req.call_count == 1

    def test_passes_through_request_kwargs(self):
        with patch("requests.request", return_value=_resp(200)) as req:
            request_with_backoff(
                "POST", "https://example.com/x",
                headers={"Authorization": "Bearer t"},
                params={"a": 1}, json_body={"b": 2}, timeout=5.0,
                sleep=lambda s: None,
            )
        req.assert_called_once_with(
            "POST", "https://example.com/x",
            headers={"Authorization": "Bearer t"},
            params={"a": 1}, json={"b": 2}, data=None, timeout=5.0,
        )
