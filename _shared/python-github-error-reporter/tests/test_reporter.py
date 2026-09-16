"""Unit tests for the Python error reporter.

Covers:
  - dedup window blocks same-key reports within the window
  - dedup releases after the window
  - distinct keys (different app OR different error_type) are not deduped
  - missing required fields → False
  - missing token → False (silent disable)
  - HTTP dispatch shape matches what the workflow expects (event_type +
    client_payload schema)
"""

from __future__ import annotations

import json
import time
from unittest import mock

import pytest

from python_github_error_reporter import GitHubErrorReporter, reporter


@pytest.fixture(autouse=True)
def _reset_reporter():
    """Reset class state between tests so they don't leak into each other."""
    GitHubErrorReporter._app = ""
    GitHubErrorReporter._environment = "production"
    GitHubErrorReporter._token = ""
    GitHubErrorReporter._repo = "example/repo"
    GitHubErrorReporter._registered = False
    GitHubErrorReporter._recent_reports = {}
    GitHubErrorReporter._dedup_window_s = 60.0
    yield
    GitHubErrorReporter._app = ""
    GitHubErrorReporter._token = ""
    GitHubErrorReporter._registered = False
    GitHubErrorReporter._recent_reports = {}


@pytest.fixture
def fake_post():
    """Patch the urllib post so tests never hit the network."""
    with mock.patch.object(reporter, "_post_with_retry", return_value=True) as m:
        yield m


def test_register_silently_disables_without_token():
    GitHubErrorReporter.register(app="test-app")
    # Even with no token, register doesn't raise.
    assert GitHubErrorReporter._registered is False
    # And subsequent report() calls return False.
    assert GitHubErrorReporter.report(error_type="X", message="y") is False


def test_report_requires_app_error_type_and_message(fake_post):
    GitHubErrorReporter._token = "fake"
    GitHubErrorReporter._app = "test-app"
    assert GitHubErrorReporter.report(error_type="", message="msg") is False
    assert GitHubErrorReporter.report(error_type="E", message="") is False
    fake_post.assert_not_called()


def test_dedup_within_window_returns_true_but_skips_dispatch(fake_post):
    GitHubErrorReporter._token = "fake"
    GitHubErrorReporter._app = "test-app"
    GitHubErrorReporter._dedup_window_s = 60.0

    # First report → dispatched
    assert GitHubErrorReporter.report(error_type="MyError", message="m1") is True
    assert fake_post.call_count == 1

    # Same (app, error_type) within window → silently skipped (returns True)
    assert GitHubErrorReporter.report(error_type="MyError", message="m2") is True
    assert fake_post.call_count == 1  # still 1, not 2


def test_different_error_type_bypasses_dedup(fake_post):
    GitHubErrorReporter._token = "fake"
    GitHubErrorReporter._app = "test-app"

    GitHubErrorReporter.report(error_type="ErrorA", message="m")
    GitHubErrorReporter.report(error_type="ErrorB", message="m")
    assert fake_post.call_count == 2


def test_different_app_bypasses_dedup(fake_post):
    GitHubErrorReporter._token = "fake"

    GitHubErrorReporter.report(app="app1", error_type="X", message="m")
    GitHubErrorReporter.report(app="app2", error_type="X", message="m")
    assert fake_post.call_count == 2


def test_dedup_releases_after_window(fake_post):
    GitHubErrorReporter._token = "fake"
    GitHubErrorReporter._app = "test-app"
    GitHubErrorReporter._dedup_window_s = 0.001  # 1 ms

    GitHubErrorReporter.report(error_type="X", message="m")
    time.sleep(0.01)
    GitHubErrorReporter.report(error_type="X", message="m")
    assert fake_post.call_count == 2


def test_dispatch_payload_schema_matches_workflow():
    """Confirm the JSON we send matches the schema the receiving workflow expects."""
    captured = {}

    def _capture(repo, token, payload):
        captured["repo"] = repo
        captured["token"] = token
        captured["payload"] = payload
        return True

    GitHubErrorReporter._token = "fake-token"
    GitHubErrorReporter._app = "test-app"
    GitHubErrorReporter._environment = "test-env"
    GitHubErrorReporter._repo = "owner/repo"

    with mock.patch.object(reporter, "_post_with_retry", side_effect=_capture):
        GitHubErrorReporter.report(
            error_type="MyError",
            message="something broke",
            stacktrace="Traceback...",
            severity="critical",
            url="https://example.com",
            extra="ctx",
        )

    assert captured["repo"] == "owner/repo"
    assert captured["token"] == "fake-token"
    payload = captured["payload"]
    assert payload["event_type"] == "production-error"
    cp = payload["client_payload"]
    assert cp["app"] == "test-app"
    assert cp["error_type"] == "MyError"
    assert cp["message"] == "something broke"
    assert cp["stacktrace"] == "Traceback..."
    assert cp["severity"] == "critical"
    assert cp["environment"] == "test-env"
    assert cp["url"] == "https://example.com"
    assert cp["extra"] == "ctx"
    assert cp["timestamp"].endswith("Z")  # ISO 8601 UTC


def test_message_and_stacktrace_truncation():
    """5000 / 10000 char caps mirror the TS reporter."""
    captured = {}

    def _capture(repo, token, payload):
        captured["payload"] = payload
        return True

    GitHubErrorReporter._token = "fake"
    GitHubErrorReporter._app = "test-app"

    with mock.patch.object(reporter, "_post_with_retry", side_effect=_capture):
        GitHubErrorReporter.report(
            error_type="X",
            message="A" * 6000,
            stacktrace="B" * 12000,
        )

    assert len(captured["payload"]["client_payload"]["message"]) == 5000
    assert len(captured["payload"]["client_payload"]["stacktrace"]) == 10000


def test_post_with_retry_returns_true_on_204():
    """Live the success path through the real _post_with_retry."""
    GitHubErrorReporter._token = "fake"
    GitHubErrorReporter._app = "test-app"

    fake_resp = mock.MagicMock()
    fake_resp.status = 204
    fake_resp.__enter__ = lambda self: self
    fake_resp.__exit__ = lambda self, *a: None

    with mock.patch("urllib.request.urlopen", return_value=fake_resp):
        ok = GitHubErrorReporter.report(error_type="X", message="m")
    assert ok is True


def test_post_with_retry_returns_false_on_persistent_failure():
    GitHubErrorReporter._token = "fake"
    GitHubErrorReporter._app = "test-app"

    with mock.patch("urllib.request.urlopen", side_effect=ConnectionError("boom")):
        with mock.patch("time.sleep"):  # skip the 2/4 s waits
            ok = GitHubErrorReporter.report(error_type="X", message="m")
    assert ok is False


def test_disabled_without_a_repository(fake_post, monkeypatch):
    # A token alone must not send anything anywhere: there is no default repo.
    monkeypatch.delenv("GITHUB_REPO", raising=False)
    GitHubErrorReporter._repo = ""
    GitHubErrorReporter.register(app="x", token="t", install_async_hook=False,
                                 install_sys_excepthook=False,
                                 install_threading_excepthook=False,
                                 install_atexit_handler=False)
    assert GitHubErrorReporter._registered is False
    assert GitHubErrorReporter.report(error_type="E", message="m") is False
    fake_post.assert_not_called()
