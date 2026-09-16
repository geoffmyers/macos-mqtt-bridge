"""GitHubErrorReporter — dispatches production errors to a GitHub repository as
`repository_dispatch` events (`event_type: production-error`), for a workflow
there to turn into issues.

Stdlib-only (urllib + threading + hashlib + atexit), so it is safe to add as a
dependency on small single-board hosts.
"""

from __future__ import annotations

import asyncio
import atexit
import hashlib
import json
import logging
import os
import sys
import threading
import time
import traceback
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Any

_LOG = logging.getLogger(__name__)

# No default target: reporting is off until a repository is configured, so a
# copy of this code never files issues anywhere by accident.
_DEFAULT_REPO = ""
_DEFAULT_DEDUP_WINDOW = 60  # seconds
_USER_AGENT = "python-github-error-reporter/0.1"
_DISPATCH_TIMEOUT_S = 10.0
_MAX_RETRIES = 3
_RETRY_INITIAL_DELAY_S = 2.0


class GitHubErrorReporter:
    """Singleton-style reporter (all state is class-level).

    Usage::

        from python_github_error_reporter import GitHubErrorReporter

        GitHubErrorReporter.register(
            app="my-bridge",
            environment="production",
        )

        # manual report:
        GitHubErrorReporter.report(
            app="my-bridge",
            error_type="FrameDecodeError",
            message="Frame decode failed",
            severity="critical",
        )
    """

    _app: str = ""
    _environment: str = "production"
    _token: str = ""
    _repo: str = _DEFAULT_REPO
    _registered: bool = False
    _install_async_hook: bool = False

    # Process-scoped dedup cache: md5(app|error_type) -> wall-clock epoch seconds.
    # Cross-process recurrences are blocked by the workflow's once-per-title no-op;
    # this in-memory cache only short-circuits tight loops within a single process.
    _recent_reports: dict[str, float] = {}
    _dedup_window_s: float = float(_DEFAULT_DEDUP_WINDOW)
    _cache_lock: threading.Lock = threading.Lock()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @classmethod
    def register(
        cls,
        app: str,
        environment: str | None = None,
        token: str | None = None,
        repo: str | None = None,
        deduplication_window: int = _DEFAULT_DEDUP_WINDOW,
        install_async_hook: bool = True,
        install_sys_excepthook: bool = True,
        install_threading_excepthook: bool = True,
        install_atexit_handler: bool = True,
    ) -> None:
        """Install error/exception handlers for automatic reporting.

        Safe to call multiple times — second call updates config without
        re-installing hooks. No-op unless both a token (``token`` or
        ``GITHUB_ERROR_TOKEN``) and a target repository (``repo`` or
        ``GITHUB_REPO``, as ``owner/name``) are set: an unconfigured reporter
        is silently disabled.
        """
        cls._app = app
        cls._environment = environment or os.environ.get(
            "GITHUB_ERROR_ENVIRONMENT", cls._environment
        )
        cls._token = token or os.environ.get("GITHUB_ERROR_TOKEN", "")
        cls._repo = repo or os.environ.get("GITHUB_REPO", _DEFAULT_REPO)
        cls._dedup_window_s = float(deduplication_window)
        cls._install_async_hook = install_async_hook

        if not cls._token or not cls._repo:
            _LOG.info(
                "GitHubErrorReporter: GITHUB_ERROR_TOKEN or GITHUB_REPO not set — "
                "error reporting disabled"
            )
            return

        if cls._registered:
            return

        if install_sys_excepthook:
            _prev_excepthook = sys.excepthook
            sys.excepthook = _make_sys_excepthook(cls, _prev_excepthook)

        if install_threading_excepthook and hasattr(threading, "excepthook"):
            _prev_thread_hook = threading.excepthook
            threading.excepthook = _make_threading_excepthook(cls, _prev_thread_hook)

        if install_atexit_handler:
            atexit.register(_make_atexit_handler(cls))

        if install_async_hook:
            cls._try_install_asyncio_hook()

        cls._registered = True
        _LOG.info(
            "GitHubErrorReporter registered for app=%s env=%s repo=%s window=%ds",
            cls._app, cls._environment, cls._repo, int(cls._dedup_window_s),
        )

    @classmethod
    def report(
        cls,
        app: str | None = None,
        error_type: str = "",
        message: str = "",
        stacktrace: str = "",
        severity: str = "error",
        url: str = "",
        extra: str = "",
        environment: str | None = None,
    ) -> bool:
        """Synchronous dispatch. Returns True on accepted (204) OR silently
        deduplicated; False on any failure or missing required field.

        Safe to call from inside an asyncio event loop — uses urllib (blocking)
        with a 10 s timeout, so worst-case event-loop stall is bounded. If you
        need full non-blocking behavior, use ``report_async`` instead.
        """
        return _do_report(
            cls,
            app=app or cls._app,
            error_type=error_type,
            message=message,
            stacktrace=stacktrace,
            severity=severity,
            url=url,
            extra=extra,
            environment=environment or cls._environment,
        )

    @classmethod
    async def report_async(
        cls,
        app: str | None = None,
        error_type: str = "",
        message: str = "",
        stacktrace: str = "",
        severity: str = "error",
        url: str = "",
        extra: str = "",
        environment: str | None = None,
    ) -> bool:
        """Async wrapper around ``report`` — runs the blocking dispatch in a
        thread so the event loop keeps spinning."""
        return await asyncio.to_thread(
            cls.report,
            app=app,
            error_type=error_type,
            message=message,
            stacktrace=stacktrace,
            severity=severity,
            url=url,
            extra=extra,
            environment=environment,
        )

    # ------------------------------------------------------------------
    # Internal hook installation
    # ------------------------------------------------------------------

    @classmethod
    def _try_install_asyncio_hook(cls) -> None:
        """Install an asyncio loop exception handler if a loop is running."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return  # No running loop; the loop's owner can call install_asyncio_hook later.
        cls.install_asyncio_hook(loop)

    @classmethod
    def install_asyncio_hook(cls, loop: asyncio.AbstractEventLoop) -> None:
        """Install the loop exception handler on a specific asyncio loop.

        Callers running an event loop should invoke this explicitly after
        ``asyncio.run(main())`` begins — e.g. inside the top-level ``main()``
        coroutine. It covers the asyncio-task surface (unhandled exceptions
        from background tasks), which ``sys.excepthook`` does not.
        """
        prev_handler = loop.get_exception_handler()
        loop.set_exception_handler(_make_asyncio_exception_handler(cls, prev_handler))


# ----------------------------------------------------------------------
# Module-level convenience wrappers
# ----------------------------------------------------------------------

def register(*args: Any, **kwargs: Any) -> None:
    GitHubErrorReporter.register(*args, **kwargs)


def report(*args: Any, **kwargs: Any) -> bool:
    return GitHubErrorReporter.report(*args, **kwargs)


async def report_async(*args: Any, **kwargs: Any) -> bool:
    return await GitHubErrorReporter.report_async(*args, **kwargs)


# ----------------------------------------------------------------------
# Internals
# ----------------------------------------------------------------------


def _do_report(
    cls: type[GitHubErrorReporter],
    *,
    app: str,
    error_type: str,
    message: str,
    stacktrace: str,
    severity: str,
    url: str,
    extra: str,
    environment: str,
) -> bool:
    if not cls._token or not cls._repo:
        return False
    if not app or not error_type or not message:
        return False

    # Dedup: identical (app, error_type) within the window → silently skip.
    # Key mirrors the workflow's title-level dedup so reporter + workflow
    # agree on what counts as "the same error".
    key = hashlib.md5(f"{app}|{error_type}".encode("utf-8")).hexdigest()
    now = time.time()
    with cls._cache_lock:
        last = cls._recent_reports.get(key)
        if last is not None and (now - last) < cls._dedup_window_s:
            return True  # silently skipped
        cls._recent_reports[key] = now
        # Prune
        cls._recent_reports = {
            k: ts for k, ts in cls._recent_reports.items()
            if (now - ts) < cls._dedup_window_s
        }

    payload = {
        "event_type": "production-error",
        "client_payload": {
            "app": app,
            "error_type": error_type,
            "message": message[:5000],
            "stacktrace": (stacktrace or "")[:10000],
            "severity": severity or "error",
            "environment": environment or "production",
            "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "url": url or "",
            "extra": extra or "",
        },
    }

    return _post_with_retry(cls._repo, cls._token, payload)


def _post_with_retry(repo: str, token: str, payload: dict[str, Any]) -> bool:
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        f"https://api.github.com/repos/{repo}/dispatches",
        data=body,
        method="POST",
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "User-Agent": _USER_AGENT,
        },
    )

    delay = _RETRY_INITIAL_DELAY_S
    for attempt in range(_MAX_RETRIES):
        try:
            with urllib.request.urlopen(req, timeout=_DISPATCH_TIMEOUT_S) as resp:
                if resp.status == 204:
                    return True
                _LOG.warning(
                    "GitHubErrorReporter: dispatch returned HTTP %d", resp.status
                )
        except urllib.error.HTTPError as e:
            _LOG.warning(
                "GitHubErrorReporter: HTTP error %d on attempt %d/%d: %s",
                e.code, attempt + 1, _MAX_RETRIES, e.reason,
            )
        except Exception as e:  # noqa: BLE001 — never let reporter break the host
            _LOG.warning(
                "GitHubErrorReporter: dispatch failed on attempt %d/%d: %s",
                attempt + 1, _MAX_RETRIES, e,
            )

        if attempt < _MAX_RETRIES - 1:
            time.sleep(delay)
            delay *= 2

    return False


def _make_sys_excepthook(cls, prev_hook):
    def _hook(exc_type, exc_value, exc_tb):
        try:
            cls.report(
                app=cls._app,
                error_type=exc_type.__name__ if exc_type else "UnhandledException",
                message=str(exc_value) if exc_value else "(no message)",
                stacktrace="".join(traceback.format_exception(exc_type, exc_value, exc_tb)),
                severity="critical",
            )
        except Exception:  # noqa: BLE001
            pass
        if prev_hook is not None:
            prev_hook(exc_type, exc_value, exc_tb)
    return _hook


def _make_threading_excepthook(cls, prev_hook):
    def _hook(args):
        try:
            cls.report(
                app=cls._app,
                error_type=args.exc_type.__name__ if args.exc_type else "ThreadException",
                message=str(args.exc_value) if args.exc_value else "(no message)",
                stacktrace="".join(traceback.format_exception(
                    args.exc_type, args.exc_value, args.exc_traceback,
                )),
                severity="critical",
                extra=f"thread={args.thread.name if args.thread else 'unknown'}",
            )
        except Exception:  # noqa: BLE001
            pass
        if prev_hook is not None:
            prev_hook(args)
    return _hook


def _make_asyncio_exception_handler(cls, prev_handler):
    def _handler(loop, context):
        try:
            exception = context.get("exception")
            if exception is not None:
                cls.report(
                    app=cls._app,
                    error_type=type(exception).__name__,
                    message=str(exception) or context.get("message", "(no message)"),
                    stacktrace="".join(traceback.format_exception(
                        type(exception), exception, exception.__traceback__,
                    )),
                    severity="critical",
                    extra=f"future={context.get('future')!r}; task={context.get('task')!r}",
                )
            else:
                # Loop signaled a non-exception error (e.g. unclosed transport).
                cls.report(
                    app=cls._app,
                    error_type="AsyncioLoopError",
                    message=context.get("message", "(no message)"),
                    severity="error",
                )
        except Exception:  # noqa: BLE001
            pass
        if prev_handler is not None:
            prev_handler(loop, context)
        else:
            # Mimic the default loop handler — log to stderr.
            loop.default_exception_handler(context)
    return _handler


def _make_atexit_handler(cls):
    def _handler():
        # Hook reserved for future buffered-flush work. Today the reporter is
        # synchronous-on-demand so atexit is a no-op — but registering it keeps
        # the door open for an outbox-backed mode without an API break.
        return
    return _handler
